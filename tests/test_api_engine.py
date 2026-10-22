"""Coroutines run under asyncio.run: neither pytest-asyncio nor pytest-anyio is installed."""
from __future__ import annotations

import asyncio
from typing import Any, cast

import anthropic
import httpx2
import pytest
from anthropic.types import (
    CacheCreation,
    ContentBlock,
    Message,
    RefusalStopDetails,
    StopReason,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    Usage,
)

from app.core.agent.api_engine import API_KEY_VARIABLE, AnthropicApiEngine
from app.core.agent.bound_tool import BoundToolSpec, bind_by_signature
from app.core.agent.engine import ChatEngine
from app.core.agent.errors import ApiKeyMissing
from app.core.agent.store import OFFER_NEXT_STEPS, TranscriptMessage

_HAIKU = "claude-haiku-5-5"
_MTOK = 1_000_000


class _FakeMessages:
    def __init__(self, replies: list[Message | Exception]) -> None:
        self._replies = replies
        self.requests: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Message:
        # The engine appends to one list across requests, so keep what THIS request held.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        reply = self._replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _FakeClient:
    def __init__(self, replies: list[Message | Exception]) -> None:
        self.messages = _FakeMessages(replies)


def _usage(
    *, uncached: int = 10, output: int = 5, read: int | None = None, written: int | None = None,
) -> Usage:
    return Usage(
        input_tokens=uncached,
        output_tokens=output,
        cache_read_input_tokens=read,
        cache_creation_input_tokens=written,
    )


def _reply(
    *content: ContentBlock,
    stop_reason: StopReason = "end_turn",
    usage: Usage | None = None,
    stop_details: RefusalStopDetails | None = None,
) -> Message:
    return Message(
        id="msg",
        type="message",
        role="assistant",
        model=_HAIKU,
        content=list(content),
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage or _usage(),
    )


def _text(text: str) -> TextBlock:
    return TextBlock(type="text", text=text)


def _call(tool_use_id: str, name: str, **arguments: Any) -> ToolUseBlock:
    return ToolUseBlock(type="tool_use", id=tool_use_id, name=name, input=arguments)


def _tool(name: str, fn: Any) -> BoundToolSpec:
    return bind_by_signature(
        name=name,
        description=f"{name} for the test",
        fn=fn,
        label=f"Running {name}",
        parameters={"stage_id": "the stage to act on"},
    )


def _read_stage(stage_id: str) -> str:
    return f"stage {stage_id} reads score"


def _engine(
    replies: list[Message | Exception],
    *,
    tools: list[BoundToolSpec] | None = None,
    max_turns: int | None = None,
) -> tuple[AnthropicApiEngine, _FakeMessages]:
    client = _FakeClient(replies)
    engine = AnthropicApiEngine(
        system_prompt="You edit workflows.",
        tools=tools if tools is not None else [_tool("read_stage", _read_stage)],
        tool_labels={"read_stage": "Reading a stage"},
        model=_HAIKU,
        effort="low",
        max_turns=max_turns,
        client=cast(anthropic.AsyncAnthropic, client),
    )
    return engine, client.messages


def _run(
    engine: ChatEngine,
    prompt: str = "Rename the score stage",
    history: list[TranscriptMessage] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    transcript, resume = asyncio.run(
        engine.stream_turn(prompt, message_history=history, emit=events.append)
    )
    assert resume is None
    return transcript, events


def test_a_text_turn_streams_its_blocks_and_bills_its_one_call() -> None:
    reply = _reply(
        ThinkingBlock(type="thinking", thinking="The reader wants a rename.", signature="sig"),
        _text("Which name?"),
        usage=_usage(uncached=1_000, output=200, read=5_000, written=300),
    )
    engine, _requests = _engine([reply])

    transcript, events = _run(engine)

    assert events == [
        {"kind": "thinking", "text": "The reader wants a rename."},
        {"kind": "text", "text": "Which name?"},
    ]
    assert transcript == [
        {"role": "user", "parts": [{"type": "text", "text": "Rename the score stage"}]},
        {"role": "assistant", "parts": [
            {"type": "thinking", "text": "The reader wants a rename."},
            {"type": "text", "text": "Which name?"},
        ]},
    ]
    usage = engine.last_usage
    assert usage is not None
    assert (usage.model, usage.input_tokens, usage.output_tokens, usage.calls) == (
        _HAIKU, 1_000, 200, 1,
    )
    input_usd = (1_000 + 300 * 1.25 + 5_000 * 0.1) * 0.10 / _MTOK
    assert usage.cost_usd == pytest.approx(input_usd + 200 * 0.50 / _MTOK)


def test_the_request_asks_for_cached_summarized_thinking_and_sets_no_sampling() -> None:
    def edit_stage(stage_id: str) -> str:
        return "edited"

    edit = _tool("edit_stage", edit_stage)
    read = _tool("read_stage", _read_stage)
    engine, requests = _engine([_reply(_text("Hi."))], tools=[edit, read])

    _run(engine, prompt="Hello")

    (request,) = requests.requests
    assert request["model"] == _HAIKU
    assert request["max_tokens"] == 16000
    assert request["system"] == [{"type": "text", "text": "You edit workflows."}]
    assert [tool["name"] for tool in request["tools"]] == ["edit_stage", "read_stage"]
    assert request["tools"][1]["input_schema"] == read.json_schema
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert request["output_config"] == {"effort": "low"}
    assert request["messages"] == [{"role": "user", "content": "Hello"}]
    assert not {"temperature", "top_p", "top_k"} & set(request)


def test_tool_calls_run_in_order_and_return_together_in_one_user_message() -> None:
    ran: list[str] = []

    def read_stage(stage_id: str) -> str:
        ran.append(stage_id)
        return f"stage {stage_id}"

    asking = _reply(
        _text("Reading both."),
        _call("tu_1", "read_stage", stage_id="score"),
        _call("tu_2", "read_stage", stage_id="clean"),
        stop_reason="tool_use",
        usage=_usage(uncached=100, output=10),
    )
    done = _reply(_text("Both read."), usage=_usage(uncached=40, output=6))
    engine, requests = _engine([asking, done], tools=[_tool("read_stage", read_stage)])

    _transcript, events = _run(engine)

    assert ran == ["score", "clean"]
    follow_up = requests.requests[1]["messages"]
    assert follow_up[1] == {"role": "assistant", "content": asking.content}
    assert follow_up[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "stage score"},
        {"type": "tool_result", "tool_use_id": "tu_2", "content": "stage clean"},
    ]}
    assert [event["kind"] for event in events] == [
        "text", "tool_call", "tool_call", "tool_result", "tool_result", "text",
    ]
    assert events[1]["label"] == "Reading a stage"
    assert engine.last_usage is not None
    assert (engine.last_usage.calls, engine.last_usage.input_tokens) == (2, 140)
    assert engine.last_usage.output_tokens == 16


def test_a_raising_tool_reaches_the_model_as_an_error_result() -> None:
    def read_stage(stage_id: str) -> str:
        raise ValueError(f"no stage {stage_id}")

    asking = _reply(_call("tu_1", "read_stage", stage_id="gone"), stop_reason="tool_use")
    engine, requests = _engine(
        [asking, _reply(_text("It is gone."))], tools=[_tool("read_stage", read_stage)],
    )

    _transcript, events = _run(engine)

    assert requests.requests[1]["messages"][2]["content"] == [{
        "type": "tool_result", "tool_use_id": "tu_1", "content": "ERROR: no stage gone",
        "is_error": True,
    }]
    assert {"kind": "tool_result", "content": "ERROR: no stage gone"} in events


def test_a_tool_nobody_offered_is_an_error_the_model_sees() -> None:
    asking = _reply(_call("tu_1", "drop_table", stage_id="score"), stop_reason="tool_use")
    engine, requests = _engine([asking, _reply(_text("I cannot."))])

    _run(engine)

    (result,) = requests.requests[1]["messages"][2]["content"]
    assert result["is_error"] is True
    assert "'drop_table'" in result["content"]
    assert "read_stage" in result["content"]


def test_offered_next_steps_draw_as_an_offer_and_hide_their_result() -> None:
    def offer_next_steps(options: list[str]) -> str:
        return "offered"

    offer_tool = bind_by_signature(
        name=OFFER_NEXT_STEPS, description="Offer replies", fn=offer_next_steps,
        label="Offering", parameters={"options": "replies the reader may send"},
    )
    asking = _reply(
        _call("tu_1", OFFER_NEXT_STEPS, options=["Run it", "Show the plan"]),
        stop_reason="tool_use",
    )
    engine, _requests = _engine([asking, _reply()], tools=[offer_tool])

    transcript, events = _run(engine)

    options = [{"text": "Run it", "url": None}, {"text": "Show the plan", "url": None}]
    assert events == [{"kind": "offer", "options": options}]
    assert transcript[1]["parts"] == [{"type": "offer", "options": options}]


def test_history_replays_as_plain_text_from_the_first_user_message() -> None:
    history: list[TranscriptMessage] = [
        {"role": "assistant", "parts": [{"type": "text", "text": "I can edit this workflow."}]},
        {"role": "user", "parts": [{"type": "text", "text": "What does score read?"}]},
        {"role": "assistant", "parts": [
            {"type": "thinking", "text": "Look it up."},
            {"type": "tool_call", "name": "read_stage", "args": '{"stage_id": "score"}',
             "label": "Reading a stage"},
            {"type": "tool_result", "content": "stage score reads clean"},
            {"type": "text", "text": "It reads clean."},
            {"type": "offer", "options": [{"text": "Rename it", "url": None},
                                          {"text": "Leave it", "url": None}]},
        ]},
        {"role": "user", "parts": [{"type": "text", "text": "  "}]},
        {"role": "assistant", "parts": [{"type": "thinking", "text": "Nothing to say."}]},
    ]
    engine, requests = _engine([_reply(_text("Renamed."))])

    _run(engine, prompt="Rename it", history=history)

    assert requests.requests[0]["messages"] == [
        {"role": "user", "content": "What does score read?"},
        {"role": "assistant", "content": (
            '[called read_stage {"stage_id": "score"} — its result is not replayed; '
            "call it again if you need it]\n\n"
            "It reads clean.\n\n"
            "[offered the reader these replies: Rename it | Leave it]"
        )},
        {"role": "user", "content": "Rename it"},
    ]


@pytest.mark.parametrize(("stop_reason", "stop_details", "named"), [
    ("max_tokens", None, ["'max_tokens'"]),
    ("refusal", RefusalStopDetails(type="refusal", category="cyber"), ["'refusal'", "'cyber'"]),
])
def test_a_reply_cut_short_ends_the_turn_with_an_error_and_keeps_its_text(
    stop_reason: StopReason, stop_details: RefusalStopDetails | None, named: list[str],
) -> None:
    cut = _reply(_text("Halfway"), stop_reason=stop_reason, stop_details=stop_details)
    engine, _requests = _engine([cut])

    transcript, events = _run(engine)

    error = events[-1]
    assert error["kind"] == "error"
    assert all(word in error["text"] for word in named)
    assert transcript[1]["parts"] == [{"type": "text", "text": "Halfway"}]


def test_the_turn_cap_stops_a_model_still_calling_tools() -> None:
    def asking(tool_use_id: str) -> Message:
        return _reply(_call(tool_use_id, "read_stage", stage_id="score"), stop_reason="tool_use")

    never_requested = _reply(_text("Done."))
    engine, requests = _engine(
        [asking("tu_1"), asking("tu_2"), never_requested], max_turns=2,
    )

    _transcript, events = _run(engine)

    assert len(requests.requests) == 2
    assert [event["kind"] for event in events].count("tool_result") == 2
    assert events[-1] == {
        "kind": "error",
        "text": "agent turn stopped after 2 model calls, with the model still calling tools",
    }


def test_usage_already_booked_survives_a_later_request_failing() -> None:
    asking = _reply(
        _call("tu_1", "read_stage", stage_id="score"),
        stop_reason="tool_use",
        usage=_usage(uncached=100, output=20),
    )
    dropped = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    engine, _requests = _engine([asking, dropped])

    with pytest.raises(anthropic.APIConnectionError):
        _run(engine)

    assert engine.last_usage is not None
    assert (engine.last_usage.calls, engine.last_usage.input_tokens) == (1, 100)


def test_a_new_turn_does_not_carry_the_last_turn_s_usage() -> None:
    failing = anthropic.APIConnectionError(
        request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    engine, _requests = _engine([_reply(_text("Hi.")), failing])
    _run(engine)

    with pytest.raises(anthropic.APIConnectionError):
        _run(engine)

    assert engine.last_usage is None


def test_a_one_hour_cache_write_has_no_price_and_raises() -> None:
    usage = Usage(
        input_tokens=10,
        output_tokens=5,
        cache_creation_input_tokens=500,
        cache_creation=CacheCreation(ephemeral_1h_input_tokens=500, ephemeral_5m_input_tokens=0),
    )
    engine, _requests = _engine([_reply(_text("Hi."), usage=usage)])

    with pytest.raises(ValueError, match="500 tokens written to the 1-hour cache"):
        _run(engine)


def test_an_unpriced_model_is_refused_before_any_call() -> None:
    with pytest.raises(ValueError, match="claude-opus-5 has no price"):
        AnthropicApiEngine(
            system_prompt="", tools=[], tool_labels={}, model="claude-opus-5", effort="low",
        )


@pytest.mark.parametrize("key", [None, "", "   "])
def test_a_missing_key_fails_the_turn_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch, key: str | None,
) -> None:
    if key is None:
        monkeypatch.delenv(API_KEY_VARIABLE, raising=False)
    else:
        monkeypatch.setenv(API_KEY_VARIABLE, key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-this-one")
    engine = AnthropicApiEngine(
        system_prompt="", tools=[], tool_labels={}, model=_HAIKU, effort="low",
    )

    with pytest.raises(ApiKeyMissing, match="CARBON_PAPER_ANTHROPIC_API_KEY") as raised:
        _run(engine)

    assert isinstance(raised.value, anthropic.AnthropicError)


def test_the_client_it_builds_holds_the_dedicated_key(monkeypatch: pytest.MonkeyPatch) -> None:
    built_with: list[str] = []

    class _BuiltClient(_FakeClient):
        def __init__(self, *, api_key: str) -> None:
            built_with.append(api_key)
            super().__init__([_reply(_text("Hi."))])

        async def __aenter__(self) -> _BuiltClient:
            return self

        async def __aexit__(self, *exc_info: object) -> None:
            return None

    monkeypatch.setenv(API_KEY_VARIABLE, "sk-carbon-paper")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-this-one")
    monkeypatch.setattr(anthropic, "AsyncAnthropic", _BuiltClient)
    engine = AnthropicApiEngine(
        system_prompt="", tools=[], tool_labels={}, model=_HAIKU, effort="low",
    )

    _run(engine)

    assert built_with == ["sk-carbon-paper"]
