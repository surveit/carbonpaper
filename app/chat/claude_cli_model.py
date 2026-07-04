"""A PydanticAI Model backed by the Claude CLI via claude-agent-sdk.

Routes chat turns through the local `claude` CLI (subscription / OAuth auth)
instead of the Anthropic API, so the chat runs on a Claude Pro/Max plan with no
API key. Reuses the CLI locator + SDK import from app.runtime.llm_agent_sdk.

Intended for local development. Per Anthropic's Feb-2026 terms, subscription
OAuth is for Claude Code / claude.ai only — swap to the API-key backend
(CW_CHAT_BACKEND=anthropic) before shipping this as a product.

Confirmed behaviour vs the Anthropic API backend:
- Streams at BLOCK granularity (a whole thinking block, then the answer), not
  token-by-token, because claude-agent-sdk yields complete message blocks.
- PydanticAI-registered tools are NOT invoked here: the CLI runs its own agent
  loop, so this backend is plain multi-turn chat + thinking. Use the API backend
  when the embedding context needs PydanticAI tools.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.settings import ModelSettings

from app.runtime import llm_agent_sdk as cas


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _render(messages: list[ModelMessage]) -> tuple[str | None, str]:
    """Flatten PydanticAI messages into (system_prompt, prompt) for one CLI call.

    claude-agent-sdk `query()` is single-shot, so prior turns are re-sent as
    conversation context and the latest user message ends the prompt.
    """
    system_parts: list[str] = []
    lines: list[str] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            for p in m.parts:
                if isinstance(p, SystemPromptPart):
                    system_parts.append(_as_text(p.content))
                elif isinstance(p, UserPromptPart):
                    lines.append(f"User: {_as_text(p.content)}")
        elif isinstance(m, ModelResponse):
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            if text:
                lines.append(f"Assistant: {text}")
    system = "\n\n".join(s for s in system_parts if s) or None
    return system, "\n\n".join(lines)


def _as_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return " ".join(c if isinstance(c, str) else str(c) for c in content)
    return str(content)


# Force extended thinking on so a thinking block is emitted every turn, rather
# than leaving it to the model's discretion (which skips it on easy prompts).
# 0 disables. Budget thinking is the current claude-agent-sdk knob; on models
# that only toggle thinking on/off it acts as on.
_THINKING_TOKENS = int(os.environ.get("CW_CHAT_THINKING_TOKENS", "4000"))


def _options(model_id: str, system: str | None):
    kw: dict = dict(model=model_id, max_turns=1, allowed_tools=[], setting_sources=[])
    if _THINKING_TOKENS > 0:
        kw["thinking"] = {"type": "enabled", "budget_tokens": _THINKING_TOKENS}
    if system:
        kw["system_prompt"] = system
    if cas._CLI_PATH:
        kw["cli_path"] = cas._CLI_PATH
    return cas.ClaudeAgentOptions(**kw)


@dataclass
class _ClaudeCLIStreamedResponse(StreamedResponse):
    _model_name: str
    _prompt: str
    _system: str | None
    _timestamp: datetime = field(default_factory=_now)

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        async for msg in cas.query(prompt=self._prompt, options=_options(self._model_name, self._system)):
            if isinstance(msg, cas.AssistantMessage):
                for block in msg.content:
                    if isinstance(block, cas.ThinkingBlock):
                        for event in self._parts_manager.handle_thinking_delta(
                            vendor_part_id="thinking", content=getattr(block, "thinking", "")
                        ):
                            yield event
                    elif isinstance(block, cas.TextBlock):
                        for event in self._parts_manager.handle_text_delta(
                            vendor_part_id="content", content=block.text
                        ):
                            yield event

    async def close_stream(self) -> None:
        # The CLI subprocess ends with the query generator; nothing to tear down.
        pass

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def provider_name(self) -> str:
        return "claude-cli"

    @property
    def provider_url(self) -> None:
        return None

    @property
    def timestamp(self) -> datetime:
        return self._timestamp


@dataclass(init=False)
class ClaudeCLIModel(Model):
    """PydanticAI model that answers via the Claude CLI (subscription auth)."""

    _model_id: str

    def __init__(self, model_id: str = "sonnet", *, settings: ModelSettings | None = None):
        self._model_id = model_id
        super().__init__(settings=settings)

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        model_settings, model_request_parameters = self.prepare_request(
            model_settings, model_request_parameters
        )
        system, prompt = _render(messages)
        thinking, text = "", ""
        async for msg in cas.query(prompt=prompt, options=_options(self._model_id, system)):
            if isinstance(msg, cas.AssistantMessage):
                for block in msg.content:
                    if isinstance(block, cas.ThinkingBlock):
                        thinking += getattr(block, "thinking", "")
                    elif isinstance(block, cas.TextBlock):
                        text += block.text
        parts: list = []
        if thinking:
            parts.append(ThinkingPart(content=thinking))
        parts.append(TextPart(content=text))
        return ModelResponse(parts=parts, model_name=self._model_id, timestamp=_now())

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ):
        model_settings, model_request_parameters = self.prepare_request(
            model_settings, model_request_parameters
        )
        system, prompt = _render(messages)
        yield _ClaudeCLIStreamedResponse(
            model_request_parameters=model_request_parameters,
            _model_name=self._model_id,
            _prompt=prompt,
            _system=system,
        )

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def system(self) -> str:
        return "claude-cli"
