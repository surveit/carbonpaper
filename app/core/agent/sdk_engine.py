"""SDK-native chat engine: drives the Claude CLI subprocess via
claude_agent_sdk.query(), mapping blocks onto normalized events.

Cross-turn memory rides on the CLI session id passed back as `resume`;
`message_history` is accepted for the turn-manager contract but UNUSED.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ThinkingConfig,
    query,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import ValidationError

# `CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows — app.core.llm_sdk probes the known install locations).
from app.core.agent.errors import AccountLimitReached
from app.core.agent.store import OFFER_NEXT_STEPS, NextSteps
from app.core.agent.usage import LlmUsage
from app.core.json_types import JsonDict
from app.core.llm_sdk import CLI_PATH as _CLI_PATH
from app.core.ids import ID

CLI_MODEL = os.environ.get("CARBON_PAPER_CHAT_CLI_MODEL", "sonnet")

# The in-process MCP server name the tools are mounted under. The CLI addresses a
# tool as f"mcp__{MCP_SERVER_NAME}__{tool_name}". Kept here (not in registry) so
# this module and the registry's server-builder agree without a circular import.
MCP_SERVER_NAME = "tools"


def _account_limit_detail(msg: Any) -> str | None:
    """The CLI's own words for an exhausted allowance, else None."""
    if getattr(msg, "api_error_status", None) != 429:
        # Gated on the status, never on the wording: the CLI has already done
        # whatever retrying was appropriate before it ends a turn on one, so a
        # 429 reaching here says the ACCOUNT is out, not that this call was
        # unlucky.
        return None
    # Carried through because only the CLI's text says WHICH allowance ran out
    # and when it resets — the whole of what the reader can act on.
    return str(getattr(msg, "result", None) or "the account is out of allowance")


def _usage_from_result(msg: Any, model: str) -> LlmUsage:
    usage = getattr(msg, "usage", None) or {}
    cost = getattr(msg, "total_cost_usd", None)
    return LlmUsage(
        model=model,
        # A usage block missing a token field means the turn reported none of
        # that kind; 0 is the true count, not a stand-in for an unknown value.
        input_tokens=int(usage.get("input_tokens", 0) or 0),   # data-default-ok: absent = zero tokens reported
        output_tokens=int(usage.get("output_tokens", 0) or 0),  # data-default-ok: absent = zero tokens reported
        cost_usd=float(cost or 0.0),
        calls=1,
    )


def _stringify(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c if isinstance(c, str) else str(c) for c in content)
    return str(content)


class ClaudeAgentSdkEngine:
    def __init__(
        self,
        *,
        system_prompt: str,
        mcp_server: Any,
        allowed_tools: list[str],
        tool_labels: dict[str, str] | None = None,
        model: str = CLI_MODEL,
        max_turns: int | None = None,
        thinking: ThinkingConfig | None = None,
        builtin_tools: list[str] | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._mcp_server = mcp_server
        self._allowed_tools = allowed_tools
        # The CLI's own built-in tools (Bash, Read, Write, WebSearch, …) this run may
        # see AT ALL, distinct from allowed_tools, which only pre-approves permission
        # for tools already on offer. Empty — the default — leaves the turn with
        # nothing but the caller's in-process MCP tools, so a structured-output run
        # cannot drift into using the assistant toolset instead of answering.
        self._builtin_tools = list(builtin_tools or [])
        # Present-tense labels shown in the chat while a tool runs, keyed by the
        # bare tool name; an unlabelled tool falls back to its bare name.
        self._tool_labels = tool_labels or {}
        self._model = model
        # A hard cap on assistant turns for this run, or None to let the agent work
        # until done (the interactive default). A headless caller that runs a bounded
        # tool loop — e.g. app.core.agent.agent.Agent's submit-and-retry — sets this so a
        # model that never produces a valid answer cannot loop forever.
        self._max_turns = max_turns
        # The CLI's own thinking setting when None. `{"type": "disabled"}` is the
        # one a classifier wants: reasoning it never reads is most of its bill.
        self._thinking = thinking
        # Token/cost usage from the most recent stream_turn's terminal
        # ResultMessage (None until one arrives). Read by the headless Agent to
        # attribute spend to the caller.
        self.last_usage: LlmUsage | None = None

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        kw: dict[str, Any] = dict(
            model=self._model,
            system_prompt=self._system_prompt,
            mcp_servers={MCP_SERVER_NAME: self._mcp_server},
            allowed_tools=self._allowed_tools,
            setting_sources=[],
            # `tools` is the base set of built-ins on offer; `[]` disables every one.
            tools=self._builtin_tools,
            # Only the mcp_servers passed here — never a project/user/plugin .mcp.json
            # the CLI would otherwise merge in, whose tools this run never asked for.
            strict_mcp_config=True,
        )
        if self._max_turns is not None:
            kw["max_turns"] = self._max_turns
        if self._thinking is not None:
            kw["thinking"] = self._thinking
        if resume:
            kw["resume"] = resume
        if _CLI_PATH is not None:
            kw["cli_path"] = _CLI_PATH
        return ClaudeAgentOptions(**kw)

    async def stream_turn(
        self,
        prompt: str,
        *,
        message_history: Any,
        emit: Callable[[dict[str, Any]], None],
        resume: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        del message_history
        # Cleared before the turn, not carried over: a turn that dies before its
        # ResultMessage arrives spent an amount nobody reported, and leaving the
        # previous turn's figure in place would bill it a second time.
        self.last_usage = None
        assistant_parts: list[dict[str, Any]] = []
        hidden_tool_result_ids: set[ID] = set()
        session_id: ID | None = None
        async for msg in query(prompt=prompt, options=self._options(resume)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ThinkingBlock):
                        # A redacted or signature-only block carries no text; emitting
                        # it opens an empty disclosure in the transcript.
                        text = getattr(block, "thinking", "")
                        if text.strip():
                            emit({"kind": "thinking", "text": text})
                            assistant_parts.append({"type": "thinking", "text": text})
                    elif isinstance(block, TextBlock):
                        emit({"kind": "text", "text": block.text})
                        assistant_parts.append({"type": "text", "text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        args = json.dumps(block.input, default=str)
                        # The CLI calls tools by their namespaced name
                        # (e.g. "mcp__tools__read_stage"); the friendly label is
                        # keyed by the bare tool name.
                        bare = block.name.rsplit("__", 1)[-1]
                        offered = _read_offered_steps(bare, block.input)
                        if offered is not None:
                            # Its own part type, so the page draws buttons and the
                            # reader is never shown the plumbing behind them.
                            hidden_tool_result_ids.add(block.id)
                            options = render_offered_options(offered)
                            emit({"kind": "offer", "options": options})
                            assistant_parts.append(
                                {"type": "offer", "options": options})
                            continue
                        if bare == "submit_answer":
                            hidden_tool_result_ids.add(block.id)
                        label = self._tool_labels.get(bare, bare)
                        emit({
                            "kind": "tool_call",
                            "name": bare,
                            "args": args,
                            "label": label,
                        })
                        assistant_parts.append({
                            "type": "tool_call",
                            "name": bare,
                            "args": args,
                            "label": label,
                        })
            elif isinstance(msg, UserMessage):
                # UserMessage.content may be a bare str (a plain user turn) or a
                # list of blocks (tool results). We only surface tool results.
                _record_tool_results(
                    msg, emit, assistant_parts, hidden_tool_result_ids
                )
            elif isinstance(msg, SystemMessage):
                # The CLI's own account of the turn — the init message carries
                # which MCP servers connected and which tools the model can
                # actually see, which is the difference between a model that
                # declined to call a tool and one that was never offered it.
                # Not part of the transcript: it is the CLI talking, not the
                # model. A caller that does not want it drops the unknown kind.
                emit({
                    "kind": "system",
                    "subtype": getattr(msg, "subtype", "") or "",
                    "text": json.dumps(getattr(msg, "data", None) or {}, default=str),
                })
            elif isinstance(msg, ResultMessage):
                # ResultMessage is terminal; let the generator exhaust naturally
                # (do NOT break — breaking aclose()s a still-running generator).
                # Capture the session id to resume next turn (conversation memory).
                session_id = getattr(msg, "session_id", None)
                self.last_usage = _usage_from_result(msg, self._model)
                # A turn can end in-band with an error (permission denial on a
                # tool, max_turns exhausted) without query() raising. Surface it
                # loudly rather than ending on a silent, empty answer.
                if getattr(msg, "is_error", False):
                    detail = (
                        getattr(msg, "result", None)
                        or getattr(msg, "subtype", "")
                        or "run ended with error"
                    )
                    emit({"kind": "error", "text": f"agent run failed: {detail}"})
                    # Raised rather than returned so no caller can mistake it for
                    # a failure worth another attempt. Usage is already captured
                    # above, so what this turn spent is still booked.
                    exhausted = _account_limit_detail(msg)
                    if exhausted is not None:
                        raise AccountLimitReached(exhausted)
        transcript = [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": assistant_parts},
        ]
        return transcript, session_id


def render_offered_options(offered: NextSteps) -> list[JsonDict]:
    """Both destinations are JSON: the SSE wire, and the stored transcript."""
    return [option.model_dump(mode="json") for option in offered.options]


def _read_offered_steps(tool_name: str, payload: Any) -> NextSteps | None:
    """None for any other tool, and for arguments that do not validate — both draw as a tool row."""
    if tool_name != OFFER_NEXT_STEPS:
        return None
    try:
        return NextSteps.model_validate(payload)
    except ValidationError:
        return None


def _record_tool_results(
    message: UserMessage,
    emit: Callable[[dict[str, Any]], None],
    assistant_parts: list[dict[str, Any]],
    hidden_tool_result_ids: set[ID],
) -> None:
    blocks = message.content if isinstance(message.content, list) else []
    for block in blocks:
        if not isinstance(block, ToolResultBlock):
            continue
        hide = block.tool_use_id in hidden_tool_result_ids
        hidden_tool_result_ids.discard(block.tool_use_id)
        if hide and not getattr(block, "is_error", False):
            continue
        content = _stringify(getattr(block, "content", ""))
        emit({"kind": "tool_result", "content": content})
        assistant_parts.append({"type": "tool_result", "content": content})
