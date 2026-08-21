"""In-process registry of running chat turns + a replayable event buffer.

A turn is a detached asyncio task: closing the tab or navigating away does not
cancel it, and a client reconnecting with ``?from=N`` replays what it missed.
Single process, in memory — the buffer does not survive a server restart.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Awaitable, Callable

from claude_agent_sdk import ClaudeSDKError

from app.core.ids import ID


class Turn:
    def __init__(self, turn_id: ID, session_id: ID):
        self.id = turn_id
        self.session_id = session_id
        self.events: list[dict[str, Any]] = []
        self.done = False
        self._waiters: list[asyncio.Event] = []

    def emit(self, event: dict) -> None:
        self.events.append(event)
        self._wake()

    def finish(self) -> None:
        self.done = True
        self._wake()

    def _wake(self) -> None:
        for w in self._waiters:
            w.set()

    async def wait(self) -> None:
        waiter = asyncio.Event()
        self._waiters.append(waiter)
        try:
            await waiter.wait()
        finally:
            self._waiters.remove(waiter)


class TurnManager:
    def __init__(self):
        self._turns: dict[str, Turn] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    def start(self, *, engine, store, session_id: ID, prompt: str,
              on_done: Callable[[], Awaitable[None]] | None = None) -> str:
        turn_id = uuid.uuid4().hex[:12]
        turn = Turn(turn_id, session_id)
        self._turns[turn_id] = turn
        history = store.load_messages(session_id)
        store.set_active_turn(session_id, turn_id)
        task = asyncio.create_task(
            self._run(turn, engine, store, session_id, prompt, history, on_done)
        )
        self._tasks[turn_id] = task
        return turn_id

    async def _run(self, turn, engine, store, session_id, prompt, history, on_done) -> None:
        try:
            resume = store.resume_token(session_id)
            messages, resume_token = await engine.stream_turn(
                prompt, message_history=history, emit=turn.emit, resume=resume
            )
            store.append_messages(session_id, messages)
            if resume_token:
                # Carry the CLI session forward so the next turn resumes this
                # conversation (the demo backend returns None — nothing to carry).
                store.set_resume_token(session_id, resume_token)
        except (ClaudeSDKError, OSError) as exc:
            # Expected failure modes of a model turn — a Claude Agent SDK error
            # (CLI not found, connection dropped, process failure) or a
            # socket/subprocess error (OSError) — reach the client as an error
            # event. A genuine bug (KeyError, ValueError, …) is NOT caught here:
            # it propagates and surfaces loudly rather than masquerading as a
            # handled model failure. The `finally` still emits `done`, so the
            # client is never left hanging.
            turn.emit({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            _record_turn_spend(engine, store, session_id)
            store.set_active_turn(session_id, None)
            if on_done is not None:
                # Post-turn completion hook (e.g. generation persisting its schemas +
                # kicking the workflow once its live turn ends). Runs on success AND
                # error; a hook failure must not break teardown, so it is surfaced as an
                # error event rather than raised.
                try:
                    await on_done()
                except Exception as exc:  # noqa: BLE001 — hook boundary: never break turn teardown
                    turn.emit({"kind": "error", "text": f"post-turn hook failed: {exc}"})
            turn.emit({"kind": "done"})
            turn.finish()

    async def stream(self, turn_id: ID, from_index: int = 0):
        turn = self._turns.get(turn_id)
        if turn is None:
            yield {"kind": "error",
                   "text": "turn not found (server restarted, or unknown id)"}
            yield {"kind": "done"}
            return
        i = max(0, from_index)
        while True:
            while i < len(turn.events):
                yield turn.events[i]
                i += 1
            if turn.done:
                return
            await turn.wait()


def _record_turn_spend(engine, store, session_id: ID) -> None:
    """Called in teardown, so a turn that errored still books what it spent getting there."""
    usage = getattr(engine, "last_usage", None)  # a custom engine need not track usage
    if usage is not None:
        # None is a turn that reported nothing, which is not a turn that cost nothing.
        store.record_turn_spend(session_id, usage)


_DEFAULT_TURN_MANAGER = TurnManager()


def default_turn_manager() -> TurnManager:
    return _DEFAULT_TURN_MANAGER
