from __future__ import annotations

import asyncio
import subprocess

import pytest

from app.core.agent import codex_availability
from app.core.agent.bound_tool import bind_by_schema, bind_by_signature
from app.core.agent.codex_engine import AgentEvent, CodexChatEngine


def test_dynamic_tool_call_streams_call_and_result(fake_codex_server) -> None:
    async def drive() -> None:
        tool = bind_by_schema(
            name="echo",
            description="Echo",
            label="Echoing",
            json_schema={"type": "object"},
            fn=lambda: "ok",
        )
        events: list[AgentEvent] = []
        messages, token = await CodexChatEngine(
            "system", [tool], fake_codex_server.command
        ).stream_turn(
            "use echo", message_history=None, emit=events.append, resume=None
        )

        assert [event["kind"] for event in events] == ["tool_call", "tool_result", "text"]
        assert events[0] == {
            "kind": "tool_call",
            "name": "echo",
            "args": "{}",
            "label": "Echoing",
        }
        assert token == "thread-1"
        assert messages[-1]["role"] == "assistant"
        assert messages[-1]["parts"] == [
            {
                "type": "tool_call",
                "name": "echo",
                "args": "{}",
                "label": "Echoing",
            },
            {"type": "tool_result", "content": '"ok"'},
            {"type": "text", "text": "done"},
        ]

        request = next(call for call in fake_codex_server.requests if call.get("id") == "tool-request-1")
        assert request["result"] == {
            "contentItems": [{"type": "inputText", "text": '"ok"'}],
            "success": True,
        }

    asyncio.run(drive())


def test_new_thread_receives_prompt_safety_and_dynamic_tools(fake_codex_server) -> None:
    async def drive() -> None:
        tool = bind_by_schema(
            name="echo",
            description="Echo",
            label="Echoing",
            json_schema={"type": "object"},
            fn=lambda: "ok",
        )
        await CodexChatEngine("system", [tool], fake_codex_server.command).stream_turn(
            "use echo", message_history=None, emit=lambda _event: None, resume=None
        )

        request = next(
            call for call in fake_codex_server.requests if call.get("method") == "thread/start"
        )
        assert request["params"] == {
            "baseInstructions": "system",
            "developerInstructions": "",
            "sandbox": "read-only",
            "approvalPolicy": "never",
            "environments": [],
            "dynamicTools": [
                {"name": "echo", "description": "Echo", "inputSchema": {"type": "object"}}
            ],
        }
        turn_request = next(
            call for call in fake_codex_server.requests if call.get("method") == "turn/start"
        )
        assert turn_request["params"] == {
            "threadId": "thread-1",
            "input": [{"type": "text", "text": "use echo"}],
        }

    asyncio.run(drive())


def test_saved_thread_is_resumed(fake_codex_server) -> None:
    async def drive() -> None:
        await CodexChatEngine("system", [], fake_codex_server.command).stream_turn(
            "again", message_history=None, emit=lambda _event: None, resume="thread-1"
        )

        methods = [request.get("method") for request in fake_codex_server.requests]
        assert "thread/resume" in methods
        assert "thread/start" not in methods

    asyncio.run(drive())


def test_codex_engine_refuses_to_start_when_subscription_is_signed_out(monkeypatch) -> None:
    monkeypatch.setattr(codex_availability.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(
        codex_availability.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode=1),
    )

    async def drive() -> None:
        engine = CodexChatEngine("system", [], ("codex", "app-server", "--stdio"))
        with pytest.raises(
            codex_availability.CodexBackendUnavailableError,
            match="isn't authenticated with a ChatGPT subscription",
        ):
            await engine.stream_turn(
                "answer", message_history=None, emit=lambda _event: None, resume=None
            )

    asyncio.run(drive())


def test_reasoning_and_text_keep_the_existing_event_shapes(fake_codex_server) -> None:
    async def drive() -> None:
        events: list[AgentEvent] = []
        messages, _token = await CodexChatEngine(
            "system", [], fake_codex_server.command_for("plain")
        ).stream_turn("answer", message_history=[], emit=events.append, resume=None)

        assert events == [
            {"kind": "thinking", "text": "thinking"},
            {"kind": "text", "text": "done"},
        ]
        assert messages[-1]["parts"] == [
            {"type": "thinking", "text": "thinking"},
            {"type": "text", "text": "done"},
        ]

    asyncio.run(drive())


def test_invalid_tool_arguments_return_an_error_content_item(fake_codex_server) -> None:
    def echo(text: str) -> str:
        return text

    async def drive() -> None:
        tool = bind_by_signature(
            name="echo",
            description="Echo",
            label="Echoing",
            parameters={"text": "Text to echo."},
            fn=echo,
        )
        events: list[AgentEvent] = []
        await CodexChatEngine("system", [tool], fake_codex_server.command).stream_turn(
            "use echo", message_history=None, emit=events.append, resume=None
        )

        assert [event["kind"] for event in events] == ["tool_call", "error", "text"]
        response = next(
            call for call in fake_codex_server.requests if call.get("id") == "tool-request-1"
        )["result"]
        assert response["success"] is False
        assert response["contentItems"][0]["text"].startswith("ERROR:")

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("mode", "expected_result"),
    [
        ("shell", {"decision": "decline"}),
        ("file", {"decision": "decline"}),
        ("permission", {"permissions": {}}),
    ],
)
def test_codex_access_requests_are_denied(
    fake_codex_server, mode: str, expected_result: dict[str, object],
) -> None:
    async def drive() -> None:
        events: list[AgentEvent] = []
        await CodexChatEngine(
            "system", [], fake_codex_server.command_for(mode)
        ).stream_turn("answer", message_history=None, emit=events.append, resume=None)

        response = next(
            call for call in fake_codex_server.requests if call.get("id") == "tool-request-1"
        )
        assert response["result"] == expected_result
        assert events[0]["kind"] == "error"

    asyncio.run(drive())


def test_mcp_server_requests_are_not_invoked(fake_codex_server) -> None:
    async def drive() -> None:
        events: list[AgentEvent] = []
        await CodexChatEngine(
            "system", [], fake_codex_server.command_for("mcp")
        ).stream_turn("answer", message_history=None, emit=events.append, resume=None)

        response = next(
            call for call in fake_codex_server.requests if call.get("id") == "tool-request-1"
        )
        assert response["error"]["code"] == -32601
        assert events[0]["kind"] == "error"

    asyncio.run(drive())
