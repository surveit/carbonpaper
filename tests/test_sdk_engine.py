"""Task 2: ClaudeAgentSdkEngine drives claude_agent_sdk.query() and maps its block
stream onto the normalized events the FE renders (thinking/text/tool_call/
tool_result). query() is mocked here — no CLI subprocess is spawned.

The plan's draft used @pytest.mark.anyio, but neither pytest-anyio nor
pytest-asyncio is installed in this repo (no anyio_backend fixture), so we drive
the coroutine with asyncio.run — mirroring tests/test_sdk_tools.py.
"""
from __future__ import annotations

import asyncio
from typing import Any

import app.agent.sdk_engine as se


class _Text:  # stand-ins matching the block interface the engine reads
    def __init__(self, text: str) -> None:
        self.text = text


class _Tool:
    def __init__(self, name: str, inp: dict[str, Any]) -> None:
        self.name, self.input = name, inp


class _Result:
    def __init__(self, content: str) -> None:
        self.content = content


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


def test_stream_turn_surfaces_in_band_result_error(monkeypatch: Any) -> None:
    """A ResultMessage with is_error=True (permission denial, max_turns) must
    emit an error event, not end on a silent 'done'."""

    class _ErrResult:
        is_error = True
        result = "permission denied for mcp__project__edit_stage"
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
    """A resume token flows into ClaudeAgentOptions so the CLI continues the
    prior conversation."""
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
