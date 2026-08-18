from __future__ import annotations

import asyncio

import pytest

from app.core.agent.codex_protocol import CodexAppServer, CodexProtocolError


def test_request_returns_response_with_its_matching_id(fake_codex_server) -> None:
    async def drive() -> None:
        server = CodexAppServer(fake_codex_server.command, {})
        try:
            await server.initialize()
            result = await server.request("thread/start", {"sandbox": "read-only"})
            assert result == {"thread": {"id": "thread-1"}}
        finally:
            await server.close()

    asyncio.run(drive())


def test_server_tool_request_reaches_the_engine(fake_codex_server) -> None:
    async def drive() -> None:
        server = CodexAppServer(fake_codex_server.command, {})
        try:
            await server.initialize()
            assert (await server.next_message())["method"] == "item/tool/call"
        finally:
            await server.close()

    asyncio.run(drive())


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("malformed", "invalid JSON"),
        ("invalid_response", "valid id"),
        ("eof", "closed stdout"),
    ],
)
def test_invalid_protocol_ends_the_message_stream_loudly(
    fake_codex_server, mode: str, message: str,
) -> None:
    async def drive() -> None:
        server = CodexAppServer(fake_codex_server.command_for(mode), {})
        try:
            await server.initialize()
            with pytest.raises(CodexProtocolError, match=message):
                await asyncio.wait_for(server.next_message(), timeout=1)
        finally:
            await server.close()

    asyncio.run(drive())
