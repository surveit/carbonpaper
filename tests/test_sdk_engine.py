"""Neither pytest-anyio nor pytest-asyncio is installed here (no anyio_backend
fixture), so coroutines are driven with asyncio.run.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import app.core.agent.sdk_engine as se
import pytest


class _Text:  # stand-ins matching the block interface the engine reads
    def __init__(self, text: str) -> None:
        self.text = text


class _Tool:
    def __init__(self, name: str, inp: dict[str, Any]) -> None:
        self.name, self.input = name, inp


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content


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
        yield _Asst([_Text("Editing."), _Tool("edit_stage", {"stage_id": "score"})])
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
        allowed_tools=["mcp__tools__edit_stage"],
        tool_labels={"edit_stage": "Editing a stage"},
    )
    transcript, session_id = asyncio.run(
        engine.stream_turn("edit score", message_history=[], emit=events.append)
    )
    assert session_id == "sess-xyz"  # captured from ResultMessage, for resume

    kinds = [(e["kind"], e.get("name") or e.get("text") or e.get("content")) for e in events]
    assert kinds == [
        ("text", "Editing."),
        ("tool_call", "edit_stage"),
        ("tool_result", "ok"),
        ("text", "Done."),
    ]
    assert transcript[0]["role"] == "user"
    tool_parts = [p for m in transcript for p in m["parts"] if p.get("type") == "tool_call"]
    assert tool_parts and tool_parts[0]["label"] == "Editing a stage"
    tool_call_ev = next(e for e in events if e["kind"] == "tool_call")
    assert tool_call_ev["label"] == "Editing a stage"


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
        result = "Bearer secret must not reach the run log"
        subtype = "authentication_error"
        api_error_status = 401
        terminal_reason = "completed"

    async def fake_query(*, prompt: str, options: Any) -> Any:
        yield _Asst([_Text("Trying.")])
        yield _ErrResult()
        raise se.ClaudeSDKError("Claude Code returned an error result: success")

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
    with pytest.raises(se.ClaudeSDKError) as exc_info:
        asyncio.run(engine.stream_turn("edit", message_history=[], emit=events.append))

    errors = [e for e in events if e["kind"] == "error"]
    assert len(errors) == 1
    assert errors[0]["text"] == (
        "Claude Code terminal error: subtype=authentication_error, "
        "api_error_status=401, terminal_reason=completed"
    )
    assert str(exc_info.value) == errors[0]["text"]
    assert "secret" not in errors[0]["text"]


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
