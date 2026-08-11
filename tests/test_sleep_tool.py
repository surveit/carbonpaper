"""The sleep tool: an in-process agent runs with the CLI's built-ins disabled, so it has
no Bash and no other way to let a background run get on with it. Without one, waiting is
a burst of identical status calls.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.agent.registry import build_mcp_server
from app.tools import shared
from app.tools.shared import MAX_SLEEP_SECONDS


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Sleeps recorded, never taken."""
    recorded: list[float] = []

    async def _sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return recorded


def test_it_sleeps_what_it_was_asked_for(slept: list[float]) -> None:
    assert asyncio.run(shared.sleep(15)) == {"slept_seconds": 15}
    assert slept == [15]


def test_a_longer_ask_is_clamped_and_says_so(slept: list[float]) -> None:
    """Clamped, not refused — but the answer reports the real duration, never the ask."""
    assert asyncio.run(shared.sleep(600)) == {"slept_seconds": MAX_SLEEP_SECONDS}
    assert slept == [MAX_SLEEP_SECONDS]


def test_a_negative_ask_sleeps_nothing(slept: list[float]) -> None:
    assert asyncio.run(shared.sleep(-5)) == {"slept_seconds": 0}
    assert slept == [0]


def test_an_agent_calls_it_through_the_tool_handler(slept: list[float]) -> None:
    """The handler awaits an awaitable result; unawaited, the model would read a coroutine."""
    [tool] = build_mcp_server(shared.bind("sleep"))[2]

    async def _call() -> dict[str, Any]:
        return await tool.handler({"seconds": 3})

    out = asyncio.run(_call())

    assert out.get("is_error") is not True
    assert '"slept_seconds": 3' in out["content"][0]["text"]
    assert slept == [3]
