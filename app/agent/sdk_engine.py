"""SDK-native chat engine. Drives the Claude CLI subprocess via
claude_agent_sdk.query() so the subscription backend (no API key) can run
in-process MCP tools; maps the block stream onto the normalized events the FE
already renders (thinking/text/tool_call/tool_result).

Cross-turn memory: each turn returns the CLI session id, which the turn manager
persists and passes back as `resume` next turn, so the model sees the whole
conversation (tool calls included) without us replaying it. message_history is
accepted for the turn-manager contract but unused — the CLI session, not a
replayed transcript, carries the memory.

Generic: the engine knows nothing about any specific agent. Its system prompt,
tool labels, allowed tools and mounted MCP server are all supplied by the caller
(see app.agent.registry.build_engine).
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)

# `CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows — app.core.llm_sdk probes the known install locations).
from app.core.llm_sdk import CLI_PATH as _CLI_PATH

CLI_MODEL = os.environ.get("CW_CHAT_CLI_MODEL", "sonnet")

# The in-process MCP server name the tools are mounted under. The CLI addresses a
# tool as f"mcp__{MCP_SERVER_NAME}__{tool_name}". Kept here (not in registry) so
# this module and the registry's server-builder agree without a circular import.
MCP_SERVER_NAME = "tools"


def _stringify(content: Any) -> str:
    """Flatten a tool-result payload to a string for the FE `tool_result` event.

    The SDK may deliver tool-result content as a str or a list of content blocks
    (dicts or str). We never fabricate — an empty/absent payload becomes "".
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c if isinstance(c, str) else str(c) for c in content)
    return str(content)


class ClaudeAgentSdkEngine:
    """Drives claude_agent_sdk.query() and maps its block stream onto the
    normalized `stream_turn(prompt, *, message_history, emit, resume)` contract the
    turn manager drives, so the subscription CLI can run its own tool loop over the
    caller-supplied in-process MCP server."""

    def __init__(
        self,
        *,
        system_prompt: str,
        mcp_server: Any,
        allowed_tools: list[str],
        tool_labels: dict[str, str] | None = None,
        model: str = CLI_MODEL,
        max_turns: int | None = None,
    ) -> None:
        self._system_prompt = system_prompt
        self._mcp_server = mcp_server
        self._allowed_tools = allowed_tools
        # Present-tense labels shown in the chat while a tool runs, keyed by the
        # bare tool name; an unlabelled tool falls back to its bare name.
        self._tool_labels = tool_labels or {}
        self._model = model
        # A hard cap on assistant turns for this run, or None to let the agent work
        # until done (the interactive default). A headless caller that runs a bounded
        # tool loop — e.g. app.agent.agent.Agent's submit-and-retry — sets this so a
        # model that never produces a valid answer cannot loop forever.
        self._max_turns = max_turns

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        # max_turns caps assistant turns when the caller set one (a bounded headless
        # run); left unset the agent works until done. `resume` continues a prior CLI
        # session so the model sees the whole conversation (the first turn passes None
        # and starts a fresh session).
        kw: dict[str, Any] = dict(
            model=self._model,
            system_prompt=self._system_prompt,
            mcp_servers={MCP_SERVER_NAME: self._mcp_server},
            allowed_tools=self._allowed_tools,
            setting_sources=[],
        )
        if self._max_turns is not None:
            kw["max_turns"] = self._max_turns
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
        # message_history is unused: cross-turn memory comes from resuming the CLI
        # session (the `resume` id), not from replaying messages. The tools read
        # durable on-disk state. Returns (transcript, session_id) — the session_id
        # is persisted so the next turn resumes this conversation.
        del message_history
        assistant_parts: list[dict[str, Any]] = []
        session_id: str | None = None
        async for msg in query(prompt=prompt, options=self._options(resume)):
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ThinkingBlock):
                        text = getattr(block, "thinking", "")
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
        transcript = [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": assistant_parts},
        ]
        return transcript, session_id
