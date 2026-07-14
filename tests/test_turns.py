"""TurnManager.start runs an optional on_done hook after the turn finishes — the seam
generation uses to persist schemas + kick the workflow once its LIVE turn ends. Driven
with asyncio.run (no pytest-asyncio in this repo), mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager


class _FakeEngine:
    """Returns a fixed (transcript, resume_token) and emits one event mid-turn."""

    def __init__(self, transcript: list[dict[str, Any]], resume_token: str | None = None) -> None:
        self._transcript = transcript
        self._resume_token = resume_token

    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        emit({"kind": "text", "text": "working"})
        return self._transcript, self._resume_token


class _RaisingEngine:
    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        raise OSError("connection dropped")


def test_start_invokes_on_done_after_the_turn(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = store.create()
    calls: list[str] = []

    async def on_done() -> None:
        calls.append("done")

    async def _drive() -> None:
        tm = TurnManager()
        engine = _FakeEngine([{"role": "assistant", "parts": [{"type": "text", "text": "working"}]}])
        turn_id = tm.start(engine=engine, store=store, session_id=sid, prompt="hi", on_done=on_done)
        await tm._tasks[turn_id]

    asyncio.run(_drive())
    assert calls == ["done"]


def test_on_done_runs_even_when_the_turn_errors(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    sid = store.create()
    calls: list[str] = []

    async def on_done() -> None:
        calls.append("done")

    async def _drive() -> None:
        tm = TurnManager()
        turn_id = tm.start(engine=_RaisingEngine(), store=store, session_id=sid, prompt="hi", on_done=on_done)
        await tm._tasks[turn_id]

    asyncio.run(_drive())
    assert calls == ["done"]  # generation's completion logic runs regardless of outcome
