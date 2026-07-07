"""SDK-native chat engine for the project editing agent. Drives the Claude CLI
subprocess via claude_agent_sdk.query() so the subscription backend (no API key)
can run the in-process MCP tools; maps the block stream onto the same normalized
events the FE already renders (thinking/text/tool_call/tool_result).

Stateless per turn: message_history is accepted (for the TurnManager contract)
but not fed back into query() — each turn is a fresh query. The editing loop
still works across turns because the tools read and write durable on-disk
project state; cross-turn LLM memory is an explicit follow-up.
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

# `_CLI_PATH` is the located Claude Code CLI (the SDK does not always find it on
# PATH on Windows); reuse llm_agent_sdk's resolution so this engine and the
# runtime backend agree on which CLI to spawn.
from app.runtime.llm_agent_sdk import _CLI_PATH
from app.chat.sdk_tools import TOOL_LABELS

CLI_MODEL = os.environ.get("CW_CHAT_CLI_MODEL", "sonnet")


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


class SdkAgentEngine:
    """Satisfies the same `stream_turn(prompt, *, message_history, emit)` contract
    as ChatEngine, but drives claude_agent_sdk.query() directly instead of
    PydanticAI, so the subscription CLI can run its own tool loop over our
    in-process MCP server."""

    def __init__(
        self,
        *,
        system_prompt: str,
        mcp_server: Any,
        allowed_tools: list[str],
        model: str = CLI_MODEL,
    ) -> None:
        self._system_prompt = system_prompt
        self._mcp_server = mcp_server
        self._allowed_tools = allowed_tools
        self._model = model

    def _options(self) -> ClaudeAgentOptions:
        # No max_turns cap: the editing agent should keep working (read → edit →
        # version → compile) until the task is done, not stop mid-way. The SDK
        # default is None (uncapped), so we simply do not set it.
        kw: dict[str, Any] = dict(
            model=self._model,
            system_prompt=self._system_prompt,
            mcp_servers={"project": self._mcp_server},
            allowed_tools=self._allowed_tools,
            setting_sources=[],
        )
        if _CLI_PATH is not None:
            kw["cli_path"] = _CLI_PATH
        return ClaudeAgentOptions(**kw)

    async def stream_turn(
        self,
        prompt: str,
        *,
        message_history: Any,
        emit: Callable[[dict[str, Any]], None],
    ) -> list[dict[str, Any]]:
        # message_history is intentionally unused: stateless per turn (see module
        # docstring). It is kept to satisfy the TurnManager contract.
        del message_history
        assistant_parts: list[dict[str, Any]] = []
        async for msg in query(prompt=prompt, options=self._options()):
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
                        # (e.g. "mcp__project__read_stage"); the friendly label
                        # is keyed by the bare tool name.
                        bare = block.name.rsplit("__", 1)[-1]
                        label = TOOL_LABELS.get(bare, bare)
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
                break
        return [
            {"role": "user", "parts": [{"type": "text", "text": prompt}]},
            {"role": "assistant", "parts": assistant_parts},
        ]
