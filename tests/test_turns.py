"""TurnManager.start runs an optional on_done hook after the turn finishes — the seam
generation uses to persist schemas + kick the workflow once its LIVE turn ends. Driven
with asyncio.run (no pytest-asyncio in this repo), mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.core.agent.api_engine import API_KEY_VARIABLE
from app.core.agent.errors import ApiKeyMissing
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


class _HistoryKeepingEngine(_FakeEngine):
    def __init__(self, asked: str, answered: str) -> None:
        super().__init__([
            {"role": "user", "parts": [{"type": "text", "text": asked}]},
            {"role": "assistant", "parts": [{"type": "text", "text": answered}]},
        ])
        self.history: Any = None

    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        self.history = message_history
        return await super().stream_turn(
            prompt, message_history=message_history, emit=emit, resume=resume,
        )


def test_a_turn_is_handed_the_earlier_turns_and_not_its_own_prompt() -> None:
    store = SessionStore()
    sid = store.create()
    first = _HistoryKeepingEngine("first", "one")
    second = _HistoryKeepingEngine("second", "two")

    async def _drive() -> None:
        tm = TurnManager()
        for engine, asked in [(first, "first"), (second, "second")]:
            turn_id = tm.start(engine=engine, store=store, session_id=sid, prompt=asked)
            await tm._tasks[turn_id]

    asyncio.run(_drive())

    assert first.history == []
    assert second.history == [
        {"role": "user", "parts": [{"type": "text", "text": "first"}]},
        {"role": "assistant", "parts": [{"type": "text", "text": "one"}]},
    ]


class _KeylessEngine:
    def __init__(self) -> None:
        self.last_usage = LlmUsage(cost_usd=0.10, calls=1, model="claude-haiku-5-5")

    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        raise ApiKeyMissing(API_KEY_VARIABLE)


class _BuggyEngine:
    async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
        raise ValueError("a bug, not a model failure")


def _run_one_turn(engine: Any, store: SessionStore, sid: str) -> list[dict[str, Any]]:
    tm = TurnManager()

    async def _drive() -> list[dict[str, Any]]:
        turn_id = tm.start(engine=engine, store=store, session_id=sid, prompt="hi")
        await tm._tasks[turn_id]
        return [event async for event in tm.stream(turn_id)]

    return asyncio.run(_drive())


def test_a_missing_api_key_ends_the_turn_on_an_error_and_books_its_spend() -> None:
    store = SessionStore()
    sid = store.create()

    events = _run_one_turn(_KeylessEngine(), store, sid)

    assert [event["kind"] for event in events] == ["error", "done"]
    assert API_KEY_VARIABLE in events[0]["text"]
    assert [turn.usage.cost_usd for turn in AgentSession.load(sid).turn_spend] == [0.10]


def test_a_bug_in_the_engine_escapes_the_turn() -> None:
    store = SessionStore()
    sid = store.create()

    with pytest.raises(ValueError, match="a bug, not a model failure"):
        _run_one_turn(_BuggyEngine(), store, sid)
