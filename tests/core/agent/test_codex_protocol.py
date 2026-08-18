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


def test_concurrent_requests_receive_out_of_order_responses_by_id(
    fake_codex_server,
) -> None:
    async def drive() -> None:
        server = CodexAppServer(fake_codex_server.command_for("out_of_order"), {})
        try:
            await server.initialize()
            first, second = await asyncio.wait_for(
                asyncio.gather(
                    server.request("request/first", {}),
                    server.request("request/second", {}),
                ),
                timeout=1,
            )
            assert first == {"method": "request/first"}
            assert second == {"method": "request/second"}
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
def test_invalid_protocol_fails_a_pending_request(
    fake_codex_server, mode: str, message: str,
) -> None:
    async def drive() -> None:
        server = CodexAppServer(fake_codex_server.command_for(mode), {})
        try:
            await server.initialize()
            with pytest.raises(CodexProtocolError, match=message):
                await asyncio.wait_for(server.request("pending", {}), timeout=1)
        finally:
            await server.close()

    asyncio.run(drive())
