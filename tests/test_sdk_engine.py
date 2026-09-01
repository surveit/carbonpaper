"""Neither pytest-anyio nor pytest-asyncio is installed here (no anyio_backend
fixture), so coroutines are driven with asyncio.run.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import app.core.agent.sdk_engine as se


class _Text:  # stand-ins matching the block interface the engine reads
    def __init__(self, text: str) -> None:
        self.text = text


class _Tool:
    def __init__(
        self, name: str, inp: dict[str, Any], tool_use_id: str = "tool-use",
    ) -> None:
        self.name, self.input, self.id = name, inp, tool_use_id


class _Result:
    def __init__(
        self,
        content: str,
        tool_use_id: str = "tool-use",
        *,
        is_error: bool = False,
    ) -> None:
        self.content, self.tool_use_id, self.is_error = content, tool_use_id, is_error


class _Think:
    def __init__(self, thinking: str) -> None:
        self.thinking = thinking


class _Asst:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _User:
    def __init__(self, content: list[Any]) -> None:
        self.content = content


class _Done:
    is_error = False
    session_id = "sess-xyz"


def test_stream_turn_maps_blocks_to_normalized_events(monkeypatch: Any) -> None:
    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([_Text("Editing."), _Tool("edit_stages", {"stage_id": "score"})])
        yield _User([_Result("ok")])
        yield _Asst([_Text("Done.")])
        yield _Done()

    # patch the SDK types the engine isinstance-checks against, plus query
    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", type("Nope", (), {}))

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp",
        mcp_server=object(),
        allowed_tools=["mcp__tools__edit_stages"],
        tool_labels={"edit_stages": "Editing a stage"},
    )
    transcript, session_id = asyncio.run(
        engine.stream_turn("edit score", message_history=[], emit=events.append)
    )
    assert session_id == "sess-xyz"  # captured from ResultMessage, for resume

    kinds = [(e["kind"], e.get("name") or e.get("text") or e.get("content")) for e in events]
    assert kinds == [
        ("text", "Editing."),
        ("tool_call", "edit_stages"),
        ("tool_result", "ok"),
        ("text", "Done."),
    ]
    assert transcript[0]["role"] == "user"
    tool_parts = [p for m in transcript for p in m["parts"] if p.get("type") == "tool_call"]
    assert tool_parts and tool_parts[0]["label"] == "Editing a stage"
    tool_call_ev = next(e for e in events if e["kind"] == "tool_call")
    assert tool_call_ev["label"] == "Editing a stage"


def test_stream_turn_drops_the_submit_answer_result_before_the_next_turn(
    monkeypatch: Any,
) -> None:
    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([_Tool("mcp__tools__submit_answer", {"x": 1}, "rejected")])
        yield _User([_Result("Missing y", "rejected", is_error=True)])
        yield _Asst([_Tool("mcp__tools__submit_answer", {"x": 1}, "accepted")])
        yield _User([_Result("Accepted", "accepted")])
        yield _Asst([_Text("I already submitted the answer.")])
        yield _Done()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", type("Nope", (), {}))

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp",
        mcp_server=object(),
        allowed_tools=["mcp__tools__submit_answer"],
    )

    transcript, _ = asyncio.run(
        engine.stream_turn("answer", message_history=[], emit=events.append)
    )

    assert [event["kind"] for event in events] == [
        "tool_call", "tool_result", "tool_call", "text",
    ]
    assert [
        part["content"]
        for message in transcript
        for part in message["parts"]
        if part["type"] == "tool_result"
    ] == ["Missing y"]


def test_stream_turn_drops_a_thinking_block_carrying_no_text(monkeypatch: Any) -> None:
    """Forwarded, it opens an empty disclosure — indistinguishable from a load failure."""

    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([_Think("   \n "), _Think("weighing it"), _Text("Done.")])
        yield _Done()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", _Think)

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp", mcp_server=object(), allowed_tools=[], tool_labels={}
    )
    transcript, _ = asyncio.run(
        engine.stream_turn("go", message_history=[], emit=events.append)
    )
    assert [(e["kind"], e.get("text")) for e in events] == [
        ("thinking", "weighing it"),
        ("text", "Done."),
    ]
    # and the empty one is not stored either, so a reload renders no block for it
    thinking_parts = [p for m in transcript for p in m["parts"] if p.get("type") == "thinking"]
    assert [p["text"] for p in thinking_parts] == ["weighing it"]


def test_stream_turn_surfaces_in_band_result_error(monkeypatch: Any) -> None:

    class _ErrResult:
        is_error = True
        result = "permission denied for mcp__tools__edit_stages"
        subtype = "error"

    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([_Text("Trying.")])
        yield _ErrResult()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _ErrResult)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", type("Nope", (), {}))

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(system_prompt="sp", mcp_server=object(), allowed_tools=[])
    asyncio.run(engine.stream_turn("edit", message_history=[], emit=events.append))

    errors = [e for e in events if e["kind"] == "error"]
    assert len(errors) == 1
    assert "permission denied" in errors[0]["text"]


def test_stream_turn_passes_resume_into_options(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def fake_query(*, prompt: str, options: Any) -> Any:
        captured["resume"] = options.resume
        yield _Asst([_Text("hi")])
        yield _Done()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", type("Nope", (), {}))

    engine = se.ClaudeAgentSdkEngine(system_prompt="sp", mcp_server=object(), allowed_tools=[])
    _transcript, session_id = asyncio.run(
        engine.stream_turn("hi", message_history=[], emit=lambda e: None, resume="prev-session")
    )
    assert captured["resume"] == "prev-session"
    assert session_id == "sess-xyz"


def test_stream_turn_emits_the_cli_init_as_a_system_event_carrying_json(
    monkeypatch: Any,
) -> None:

    class _System:
        subtype = "init"
        data = {"subtype": "init", "tools": ["Bash", "mcp__tools__submit_answer"],
                "mcp_servers": [{"name": "tools", "status": "connected"}]}

    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _System()
        yield _Done()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "SystemMessage", _System)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(system_prompt="sp", mcp_server=object(), allowed_tools=[])
    asyncio.run(engine.stream_turn("hi", message_history=[], emit=events.append))

    system_events = [e for e in events if e["kind"] == "system"]
    assert len(system_events) == 1
    assert system_events[0]["subtype"] == "init"
    body = json.loads(system_events[0]["text"])
    assert body["tools"] == ["Bash", "mcp__tools__submit_answer"]
    assert body["mcp_servers"] == [{"name": "tools", "status": "connected"}]


def test_options_disable_every_builtin_tool_by_default() -> None:
    """`allowed_tools` only pre-approves permission; `tools` decides what the turn can see."""
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp",
        mcp_server=object(),
        allowed_tools=["mcp__tools__submit_answer"],
    )
    options = engine._options(None)
    assert options.tools == []
    assert options.allowed_tools == ["mcp__tools__submit_answer"]


def test_options_forward_the_builtin_tools_a_caller_asked_for() -> None:
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp",
        mcp_server=object(),
        allowed_tools=[],
        builtin_tools=["WebSearch", "WebFetch"],
    )
    assert engine._options(None).tools == ["WebSearch", "WebFetch"]


def test_options_load_no_mcp_servers_but_the_one_passed_in() -> None:
    """Otherwise the CLI merges every project/user/plugin .mcp.json server into the run."""
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp", mcp_server=object(), allowed_tools=[]
    )
    assert engine._options(None).strict_mcp_config is True


def test_an_offer_survives_the_json_the_sse_route_writes(monkeypatch: Any) -> None:
    # A model on this wire kills the SSE connection, losing every event after it.
    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([
            _Text("Take a look at any of these."),
            _Tool(f"mcp__tools__{se.OFFER_NEXT_STEPS}", {"options": [
                {"text": "Open the review queue", "url": "/project/p/runs/r/queue/s"},
                {"text": "Tell me more first"},
            ]}),
        ])
        yield _Asst([_Text("I'll be here.")])
        yield _Done()

    monkeypatch.setattr(se, "query", fake_query)
    monkeypatch.setattr(se, "AssistantMessage", _Asst)
    monkeypatch.setattr(se, "UserMessage", _User)
    monkeypatch.setattr(se, "ResultMessage", _Done)
    monkeypatch.setattr(se, "TextBlock", _Text)
    monkeypatch.setattr(se, "ToolUseBlock", _Tool)
    monkeypatch.setattr(se, "ToolResultBlock", _Result)
    monkeypatch.setattr(se, "ThinkingBlock", type("Nope", (), {}))

    events: list[dict[str, Any]] = []
    engine = se.ClaudeAgentSdkEngine(
        system_prompt="sp", mcp_server=object(), allowed_tools=[], tool_labels={})
    transcript, _ = asyncio.run(
        engine.stream_turn("where next?", message_history=[], emit=events.append))

    on_the_wire = [json.loads(json.dumps(event)) for event in events]
    offer = next(e for e in on_the_wire if e["kind"] == "offer")
    assert [o["text"] for o in offer["options"]] == [
        "Open the review queue", "Tell me more first"]
    assert offer["options"][0]["url"] == "/project/p/runs/r/queue/s"
    # The text after it is what a reader loses when the connection dies at the offer.
    assert [e["text"] for e in on_the_wire if e["kind"] == "text"] == [
        "Take a look at any of these.", "I'll be here."]
    stored = [p for m in transcript for p in m["parts"] if p.get("type") == "offer"]
    assert stored and stored[0]["options"][0]["text"] == "Open the review queue"
