"""Chat engine on the Anthropic Messages API: this process runs the tool loop, not a CLI."""
from __future__ import annotations

import contextlib
import itertools
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, Callable, Literal, TypeAlias

import anthropic
from anthropic.types import (
    ContentBlock,
    Message,
    MessageParam,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolParam,
    ToolResultBlockParam,
    ToolUseBlock,
    Usage,
)

from app.core.agent.bound_tool import BoundToolSpec, ToolOutcome, call_bound_tool
from app.core.agent.engine import TurnRecorder
from app.core.agent.errors import ApiKeyMissing
from app.core.agent.prices import RequestTokens, price_request, validate_priced
from app.core.agent.store import MessageRole, Offer, PartType, ToolCall, TranscriptMessage
from app.core.agent.usage import LlmUsage
from app.core.ids import ID

# Not ANTHROPIC_API_KEY: that would put every claude CLI on this host on metered billing.
API_KEY_VARIABLE = "CARBON_PAPER_ANTHROPIC_API_KEY"

ApiEffort: TypeAlias = Literal["low", "medium", "high"]

_TURN_FINISHED: frozenset[StopReason] = frozenset({"end_turn", "stop_sequence"})


class AnthropicApiEngine:
    def __init__(
        self,
        *,
        system_prompt: str,
        tools: list[BoundToolSpec],
        tool_labels: dict[str, str],
        model: str,
        effort: ApiEffort,
        max_tokens: int = 16000,
        max_turns: int | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        validate_priced(model)
        self._system_prompt = system_prompt
        self._tools = {spec.name: spec for spec in tools}
        self._tool_labels = tool_labels
        self._model = model
        self._effort = effort
        self._max_tokens = max_tokens
        self._max_turns = max_turns
        self._client = client
        self.last_usage: LlmUsage | None = None

    async def stream_turn(
        self,
        prompt: str,
        *,
        message_history: Sequence[TranscriptMessage] | None,
        emit: Callable[[dict[str, Any]], None],
        resume: str | None = None,
    ) -> tuple[list[dict[str, Any]], None]:
        del resume
        self.last_usage = None
        recorder = TurnRecorder(emit=emit, tool_labels=self._tool_labels)
        messages: list[MessageParam] = [
            *_replay_history(message_history or []),
            {"role": "user", "content": prompt},
        ]
        async with self._open_client() as client:
            await self._run_tool_loop(client, messages, recorder, emit)
        return recorder.build_transcript(prompt), None

    async def _run_tool_loop(
        self,
        client: anthropic.AsyncAnthropic,
        messages: list[MessageParam],
        recorder: TurnRecorder,
        emit: Callable[[dict[str, Any]], None],
    ) -> None:
        for calls_made in itertools.count(1):
            reply = await self._ask_model(client, messages)
            self._add_spend(reply.usage)
            _record_reply(reply.content, recorder)
            # Verbatim: a thinking block goes back unchanged within the turn that produced it.
            messages.append({"role": "assistant", "content": reply.content})
            if reply.stop_reason in _TURN_FINISHED:
                return
            if reply.stop_reason != "tool_use":
                emit({"kind": "error", "text": _describe_early_stop(reply)})
                return
            results = await self._run_tools(reply.content, recorder)
            messages.append({"role": "user", "content": results})
            if self._max_turns is not None and calls_made >= self._max_turns:
                emit({"kind": "error", "text": (
                    f"agent turn stopped after {calls_made} model calls, with the model "
                    "still calling tools"
                )})
                return

    async def _ask_model(
        self, client: anthropic.AsyncAnthropic, messages: list[MessageParam],
    ) -> Message:
        return await client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=[{"type": "text", "text": self._system_prompt}],
            tools=[_render_tool_param(spec) for spec in self._tools.values()],
            messages=messages,
            # The default display returns empty thinking text; the chat shows the reasoning.
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self._effort},
            cache_control={"type": "ephemeral"},
        )

    async def _run_tools(
        self, content: list[ContentBlock], recorder: TurnRecorder,
    ) -> list[ToolResultBlockParam]:
        results: list[ToolResultBlockParam] = []
        # One at a time, in the order asked: the editing tools mutate one shared draft.
        for block in content:
            if isinstance(block, ToolUseBlock):
                outcome = await self._call_tool(block)
                recorder.record_tool_result(block.id, outcome.text, is_error=outcome.is_error)
                results.append(_render_tool_result(block.id, outcome))
        return results

    async def _call_tool(self, block: ToolUseBlock) -> ToolOutcome:
        spec = self._tools.get(block.name)
        if spec is None:
            return ToolOutcome(
                text=f"ERROR: no tool named {block.name!r} is offered; the tools are "
                f"{', '.join(self._tools)}",
                is_error=True,
            )
        return await call_bound_tool(spec, block.input)

    def _add_spend(self, usage: Usage) -> None:
        spend = _price_reply(self._model, usage)
        self.last_usage = spend if self.last_usage is None else self.last_usage + spend

    @contextlib.asynccontextmanager
    async def _open_client(self) -> AsyncIterator[anthropic.AsyncAnthropic]:
        if self._client is not None:
            yield self._client
            return
        async with anthropic.AsyncAnthropic(api_key=_read_api_key()) as client:
            yield client


def _replay_history(history: Sequence[TranscriptMessage]) -> list[MessageParam]:
    """Plain text only: a replayed thinking block is bound to the exact prefix that produced it."""
    from_first_user = _drop_leading_assistant_messages(history)
    replayed = [_replay_message(message) for message in from_first_user]
    return [message for message in replayed if message is not None]


def _drop_leading_assistant_messages(
    history: Sequence[TranscriptMessage],
) -> Sequence[TranscriptMessage]:
    """The API takes a user message first; a written opening turn is in the system prompt."""
    first_user = next(
        (i for i, message in enumerate(history) if message["role"] == MessageRole.user),
        len(history),
    )
    return history[first_user:]


def _replay_message(message: TranscriptMessage) -> MessageParam | None:
    """None for a message with no text, since the API refuses an empty content block."""
    if MessageRole(message["role"]) is MessageRole.user:
        text = "\n\n".join(
            part["text"] for part in message["parts"] if PartType(part["type"]) is PartType.text
        )
        return {"role": "user", "content": text} if text.strip() else None
    lines = [_render_reply_part(part) for part in message["parts"]]
    text = "\n\n".join(line for line in lines if line)
    return {"role": "assistant", "content": text} if text.strip() else None


def _render_reply_part(part: dict[str, Any]) -> str:
    match PartType(part["type"]):
        case PartType.text:
            return str(part["text"])
        case PartType.tool_call:
            call = ToolCall.model_validate(part)
            return (
                f"[called {call.name} {call.args} — its result is not replayed; "
                "call it again if you need it]"
            )
        case PartType.offer:
            offered = [Offer.model_validate(option).text for option in part["options"]]
            return f"[offered the reader these replies: {' | '.join(offered)}]"
        case PartType.thinking | PartType.tool_result:
            return ""


def _record_reply(content: list[ContentBlock], recorder: TurnRecorder) -> None:
    for block in content:
        if isinstance(block, ThinkingBlock):
            recorder.record_thinking(block.thinking)
        elif isinstance(block, TextBlock):
            recorder.record_text(block.text)
        elif isinstance(block, ToolUseBlock):
            recorder.record_tool_call(block.id, block.name, block.input)


def _render_tool_param(spec: BoundToolSpec) -> ToolParam:
    return {"name": spec.name, "description": spec.description, "input_schema": spec.json_schema}


def _render_tool_result(tool_use_id: ID, outcome: ToolOutcome) -> ToolResultBlockParam:
    result: ToolResultBlockParam = {
        "type": "tool_result", "tool_use_id": tool_use_id, "content": outcome.text,
    }
    if outcome.is_error:
        result["is_error"] = True
    return result


def _describe_early_stop(reply: Message) -> str:
    detail = f"agent turn stopped before the model finished: stop reason {reply.stop_reason!r}"
    if reply.stop_details is not None and reply.stop_details.category is not None:
        detail += f", refusal category {reply.stop_details.category!r}"
    return detail


def _price_reply(model: str, usage: Usage) -> LlmUsage:
    cache_reads = usage.cache_read_input_tokens or 0  # data-default-ok: None = none reported
    tokens = RequestTokens(
        input_tokens=usage.input_tokens,
        cache_write_5m_input_tokens=_read_cache_writes(usage),
        cache_read_input_tokens=cache_reads,
        output_tokens=usage.output_tokens,
    )
    return LlmUsage(
        model=model,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=price_request(model, tokens),
        calls=1,
    )


def _read_cache_writes(usage: Usage) -> int:
    breakdown = usage.cache_creation
    if breakdown is not None and breakdown.ephemeral_1h_input_tokens:
        raise ValueError(
            f"the reply reports {breakdown.ephemeral_1h_input_tokens} tokens written to the "
            "1-hour cache, which has no price in app/core/agent/prices.py; this engine only "
            "asks for the 5-minute cache"
        )
    return usage.cache_creation_input_tokens or 0  # data-default-ok: None = none reported


def _read_api_key() -> str:
    key = os.environ.get(API_KEY_VARIABLE, "")
    if not key.strip():
        raise ApiKeyMissing(API_KEY_VARIABLE)
    return key
