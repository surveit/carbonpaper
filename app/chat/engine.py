"""Reusable chat engine (PydanticAI) — the generic, embeddable core.

A ``ChatEngine`` wraps a PydanticAI ``Agent`` parameterised by a system prompt
and the tools the *host* supplies for the context it embeds the chat in
(methodology authoring, run interrogation, review, ...). The engine is
context-agnostic; tools are plugged in per embedding.

``stream_turn`` runs one turn, calling ``emit(event)`` for each normalised UI
event, and returns the full message list for the caller to persist. The event
shapes mirror app.runtime.llm_agent_sdk's on_event kinds:
    {"kind": "thinking", "text": ...}     one streamed thinking chunk
    {"kind": "text",     "text": ...}     one streamed answer chunk
    {"kind": "tool_call", "name":.., "args": ..}
    {"kind": "tool_result", "content": ..}

Backend selection (``build_model``) follows app.runtime.options' discipline:
``CW_CHAT_BACKEND=dev`` picks the scripted dev model (no real LLM); otherwise the
Anthropic API is used and a missing ANTHROPIC_API_KEY raises rather than silently
falling back.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from pydantic_ai import Agent
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from .dev_model import make_dev_model

DEFAULT_ANTHROPIC_MODEL = os.environ.get("CW_CHAT_MODEL", "claude-sonnet-4-6")
CLI_MODEL = os.environ.get("CW_CHAT_CLI_MODEL", "sonnet")


class ChatBackendError(RuntimeError):
    """No usable chat model backend (e.g. real backend requested but no key)."""


def _backend_choice(backend: str | None) -> str:
    return (backend or os.environ.get("CW_CHAT_BACKEND", "auto")).strip().lower()


def build_model(backend: str | None = None):
    choice = _backend_choice(backend)
    if choice == "dev":
        return make_dev_model()
    if choice == "claude_cli":
        return _build_claude_cli()
    if choice == "anthropic":
        return _build_anthropic()
    if choice == "auto":
        # Prefer the subscription CLI (no API key, no per-token billing); fall
        # back to the API backend only if a key is configured.
        from app.runtime import llm_agent_sdk
        if llm_agent_sdk.available():
            return _build_claude_cli()
        return _build_anthropic()
    raise ChatBackendError(f"unknown CW_CHAT_BACKEND={choice!r}")


def _build_claude_cli():
    from app.runtime import llm_agent_sdk
    if not llm_agent_sdk.available():
        raise ChatBackendError(
            "claude_cli backend: the Claude CLI / Agent SDK isn't available. "
            "Install + `claude login`, or use CW_CHAT_BACKEND=anthropic with a "
            "key, or CW_CHAT_BACKEND=dev for the scripted dev model."
        )
    from .claude_cli_model import ClaudeCLIModel
    return ClaudeCLIModel(CLI_MODEL)


def _build_anthropic():
    # Read a chat-specific key so enabling the API backend does NOT require
    # setting ANTHROPIC_API_KEY process-wide: if that global var is set, the
    # Claude CLI / Agent SDK used by llm_transform (and the claude_cli backend)
    # switches from the Claude subscription to per-token API billing. Passing the
    # key explicitly here keeps the backends' billing separate.
    api_key = os.environ.get("CW_CHAT_ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ChatBackendError(
            "Anthropic API backend needs a key. Set CW_CHAT_ANTHROPIC_API_KEY, or "
            "use CW_CHAT_BACKEND=claude_cli (subscription, no key) / =dev (scripted)."
        )
    from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # Adaptive extended thinking for current Claude models. Requires a
    # thinking-capable model; not exercised in a keyless build.
    return AnthropicModel(
        DEFAULT_ANTHROPIC_MODEL,
        provider=AnthropicProvider(api_key=api_key),
        settings=AnthropicModelSettings(anthropic_thinking={"type": "adaptive"}),
    )


def backend_label(backend: str | None = None) -> str:
    choice = _backend_choice(backend)
    if choice == "dev":
        return "dev (scripted - not a real LLM)"
    if choice == "claude_cli":
        return f"claude-cli:{CLI_MODEL} (subscription)"
    if choice == "anthropic":
        return f"anthropic:{DEFAULT_ANTHROPIC_MODEL} (API key)"
    # auto
    from app.runtime import llm_agent_sdk
    if llm_agent_sdk.available():
        return f"claude-cli:{CLI_MODEL} (subscription)"
    return f"anthropic:{DEFAULT_ANTHROPIC_MODEL} (API key)"


class ChatEngine:
    """A generic chat agent. Construct one per embedding context, passing that
    context's system prompt and tools."""

    def __init__(
        self,
        *,
        system_prompt: str,
        tools: list[Callable[..., Any]] | None = None,
        toolsets: list | None = None,
        model=None,
    ):
        self.agent = Agent(
            model or build_model(),
            system_prompt=system_prompt,
            tools=tools or [],
            toolsets=toolsets or [],
        )

    async def stream_turn(self, prompt: str, *, message_history, emit):
        """Run one turn. `emit` is called synchronously per UI event. Returns the
        full message list (prior history + this turn) for persistence."""
        async with self.agent.iter(prompt, message_history=message_history) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for ev in stream:
                            _emit_part_event(ev, emit)
                elif Agent.is_call_tools_node(node):
                    async with node.stream(run.ctx) as stream:
                        async for ev in stream:
                            _emit_tool_event(ev, emit)
        return run.result.all_messages()


def _emit_part_event(ev, emit) -> None:
    if isinstance(ev, PartStartEvent):
        part = ev.part
        if isinstance(part, ThinkingPart) and part.content:
            emit({"kind": "thinking", "text": part.content})
        elif isinstance(part, TextPart) and part.content:
            emit({"kind": "text", "text": part.content})
    elif isinstance(ev, PartDeltaEvent):
        delta = ev.delta
        if isinstance(delta, ThinkingPartDelta) and delta.content_delta:
            emit({"kind": "thinking", "text": delta.content_delta})
        elif isinstance(delta, TextPartDelta) and delta.content_delta:
            emit({"kind": "text", "text": delta.content_delta})


def _emit_tool_event(ev, emit) -> None:
    if isinstance(ev, FunctionToolCallEvent):
        emit({"kind": "tool_call", "name": ev.part.tool_name,
              "args": ev.part.args_as_json_str()})
    elif isinstance(ev, FunctionToolResultEvent):
        emit({"kind": "tool_result", "content": _stringify(getattr(ev.part, "content", ""))})


def _stringify(v) -> str:
    if isinstance(v, str):
        return v
    try:
        return json.dumps(v, default=str)
    except (TypeError, ValueError):
        return str(v)
