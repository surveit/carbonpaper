"""TurnManager.start runs an optional on_done hook after the turn finishes — the seam
generation uses to persist schemas + kick the workflow once its LIVE turn ends. Driven
with asyncio.run (no pytest-asyncio in this repo), mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
import subprocess
from typing import Any

from app.core.agent import codex_availability
from app.core.agent.codex_engine import CodexChatEngine
from app.core.agent.codex_protocol import CodexProtocolError
from app.core.agent.store import AgentSession, ProseBlock, SessionStore
from app.core.agent.turns import TurnManager
from app.core.agent.usage import LlmUsage


class _FakeEngine:

    def __init__(self, transcript: list[dict[str, Any]], resume_token: str | None = None) -> None:
        self._transcript = transcript
        self._resume_token = resume_token

    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        emit({"kind": "text", "text": "working"})
        return self._transcript, self._resume_token


class _RaisingEngine:
    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        raise OSError("connection dropped")


class _ProtocolRaisingEngine:
    async def stream_turn(
        self, prompt: str, *, message_history: Any, emit: Any, resume: Any
    ):
        raise CodexProtocolError("invalid app-server message")


def test_start_invokes_on_done_after_the_turn() -> None:
    store = SessionStore()
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


def test_a_second_turn_leaves_the_first_one_on_the_page() -> None:
    """The engine reports only the turn it just ran, so a store that replaced lost the rest."""
    store = SessionStore()
    sid = store.create()

    def _turn(asked: str, answered: str) -> _FakeEngine:
        return _FakeEngine([
            {"role": "user", "parts": [{"type": "text", "text": asked}]},
            {"role": "assistant", "parts": [{"type": "text", "text": answered}]},
        ])

    async def _drive() -> None:
        tm = TurnManager()
        for asked, answered in [("first", "one"), ("second", "two")]:
            turn_id = tm.start(engine=_turn(asked, answered), store=store,
                               session_id=sid, prompt=asked)
            await tm._tasks[turn_id]

    asyncio.run(_drive())
    spoken = [block.text for bubble in store.history_view(sid)
              for block in bubble.blocks if isinstance(block, ProseBlock)]
    assert spoken == ["first", "one", "second", "two"]


def test_on_done_runs_even_when_the_turn_errors() -> None:
    store = SessionStore()
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


def test_codex_protocol_failures_reach_the_turn_stream() -> None:
    store = SessionStore()
    sid = store.create()

    async def _drive() -> list[dict[str, Any]]:
        manager = TurnManager()
        turn_id = manager.start(
            engine=_ProtocolRaisingEngine(), store=store, session_id=sid, prompt="hi"
        )
        await manager._tasks[turn_id]
        return manager._turns[turn_id].events

    assert asyncio.run(_drive()) == [
        {"kind": "error", "text": "CodexProtocolError: invalid app-server message"},
        {"kind": "done"},
    ]


def test_codex_signout_after_preflight_reaches_the_turn_stream(monkeypatch) -> None:
    store = SessionStore()
    sid = store.create()
    statuses = iter([0, 1])
    monkeypatch.setattr(codex_availability.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(
        codex_availability.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode=next(statuses)),
    )
    codex_availability.require_codex_backend()

    async def _drive() -> list[dict[str, Any]]:
        manager = TurnManager()
        turn_id = manager.start(
            engine=CodexChatEngine("system", [], ("codex", "app-server", "--stdio")),
            store=store,
            session_id=sid,
            prompt="hi",
        )
        await manager._tasks[turn_id]
        return manager._turns[turn_id].events

    assert asyncio.run(_drive()) == [
        {
            "kind": "error",
            "text": "CodexBackendUnavailableError: Codex isn't authenticated with a "
            "ChatGPT subscription. Run `codex login` before starting a chat.",
        },
        {"kind": "done"},
    ]


class _SpendingEngine(_FakeEngine):
    def __init__(self, cost_usd: float) -> None:
        super().__init__([{"role": "assistant", "parts": [{"type": "text", "text": "ok"}]}])
        self.last_usage = LlmUsage(cost_usd=cost_usd, calls=1, model="claude-sonnet-5")


class _RaisingSpendingEngine(_RaisingEngine):
    def __init__(self, cost_usd: float) -> None:
        self.last_usage = LlmUsage(cost_usd=cost_usd, calls=1, model="claude-sonnet-5")


def _spend_booked(store: SessionStore, sid: str, engine: Any) -> list[float]:
    async def _drive() -> None:
        tm = TurnManager()
        turn_id = tm.start(engine=engine, store=store, session_id=sid, prompt="hi")
        await tm._tasks[turn_id]

    asyncio.run(_drive())
    return [turn.usage.cost_usd for turn in AgentSession.load(sid).turn_spend]


def test_each_turn_books_what_it_spent_onto_the_session() -> None:
    store = SessionStore()
    sid = store.create()

    _spend_booked(store, sid, _SpendingEngine(0.25))

    assert _spend_booked(store, sid, _SpendingEngine(0.50)) == [0.25, 0.50]


def test_a_turn_that_errored_still_books_what_it_spent_getting_there() -> None:
    store = SessionStore()
    sid = store.create()

    assert _spend_booked(store, sid, _RaisingSpendingEngine(0.10)) == [0.10]


def test_an_engine_that_tracks_no_usage_books_nothing() -> None:
    store = SessionStore()
    sid = store.create()
    silent = _FakeEngine([{"role": "assistant", "parts": [{"type": "text", "text": "ok"}]}])

    assert _spend_booked(store, sid, silent) == []
