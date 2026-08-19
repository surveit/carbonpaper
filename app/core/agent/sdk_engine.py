"""SDK-native chat engine: drives the Claude CLI subprocess via
claude_agent_sdk.query(), mapping blocks onto normalized events.

Cross-turn memory rides on the CLI session id passed back as `resume`;
`message_history` is accepted for the turn-manager contract but UNUSED.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any, AsyncIterator, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
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

# `CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows — app.core.llm_sdk probes the known install locations).
from app.core.agent.usage import LlmUsage
from app.core.llm_sdk import CLI_PATH as _CLI_PATH

CLI_MODEL = os.environ.get("CARBON_PAPER_CHAT_CLI_MODEL", "sonnet")

# The in-process MCP server name the tools are mounted under. The CLI addresses a
# tool as f"mcp__{MCP_SERVER_NAME}__{tool_name}". Kept here (not in registry) so
# this module and the registry's server-builder agree without a circular import.
MCP_SERVER_NAME = "tools"


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


def _format_terminal_error(msg: ResultMessage) -> str:
    detail = f"Claude Code terminal error: subtype={getattr(msg, 'subtype', '') or 'unknown'}"
    status = getattr(msg, "api_error_status", None)
    reason = getattr(msg, "terminal_reason", None)
    if status is not None:
        detail += f", api_error_status={status}"
    if reason:
        detail += f", terminal_reason={reason}"
    return detail


async def _query_with_terminal_error(
    prompt: str, options: ClaudeAgentOptions,
) -> AsyncIterator[Any]:
    terminal_error: str | None = None
    try:
        async for msg in query(prompt=prompt, options=options):
            if isinstance(msg, ResultMessage) and getattr(msg, "is_error", False):
                terminal_error = _format_terminal_error(msg)
            yield msg
    except ClaudeSDKError as exc:
        if terminal_error is None:
            raise
        raise ClaudeSDKError(terminal_error) from exc


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
        session_id: str | None = None
        async for msg in _stream_messages_with_coalesced_telemetry(
            prompt, self._options(resume), emit
        ):
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
                blocks = msg.content if isinstance(msg.content, list) else []
                for block in blocks:
                    if isinstance(block, ToolResultBlock):
                        content = _stringify(getattr(block, "content", ""))
                        emit({"kind": "tool_result", "content": content})
                        assistant_parts.append(
                            {"type": "tool_result", "content": content}
                        )
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
                    emit({"kind": "error", "text": _format_terminal_error(msg)})
        transcript = [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": assistant_parts},
        ]
        return transcript, session_id


_THINKING_TOKENS_INTERVAL_S = 1.0


class _SystemTelemetry:
    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._emit = emit
        self._now = now
        self._last_thinking_tokens_at: float | None = None
        self._pending_thinking_tokens: dict[str, Any] | None = None

    def receive(self, msg: SystemMessage) -> None:
        event = _build_system_event(msg)
        if event["subtype"] != "thinking_tokens":
            self._emit(event)
            return
        now = self._now()
        last = self._last_thinking_tokens_at
        if last is None or now - last >= _THINKING_TOKENS_INTERVAL_S:
            self._emit(event)
            self._last_thinking_tokens_at = now
            self._pending_thinking_tokens = None
            return
        self._pending_thinking_tokens = event

    def flush(self) -> None:
        if self._pending_thinking_tokens is None:
            return
        self._emit(self._pending_thinking_tokens)
        self._pending_thinking_tokens = None


async def _stream_messages_with_coalesced_telemetry(
    prompt: str,
    options: ClaudeAgentOptions,
    emit: Callable[[dict[str, Any]], None],
) -> AsyncIterator[Any]:
    telemetry = _SystemTelemetry(emit)
    try:
        async for msg in _query_with_terminal_error(prompt, options):
            if isinstance(msg, SystemMessage):
                telemetry.receive(msg)
                continue
            if isinstance(msg, ResultMessage):
                telemetry.flush()
            yield msg
    finally:
        telemetry.flush()


def _build_system_event(msg: SystemMessage) -> dict[str, Any]:
    return {
        "kind": "system",
        "subtype": getattr(msg, "subtype", "") or "",
        "text": json.dumps(getattr(msg, "data", None) or {}, default=str),
    }
