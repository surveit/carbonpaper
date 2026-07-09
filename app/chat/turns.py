"""In-process registry of running chat turns + a replayable event buffer.

A turn runs as a detached asyncio task on the server loop, independent of any
HTTP request: closing the tab or navigating away does not cancel it. Each
streamed event is appended to an in-memory buffer, so a client that reconnects
(``?from=N``) replays what it missed and then follows live. This is what makes
"navigate away and come back to the same in-flight answer" work.

Scope: single process, in memory. The buffer does not survive a server restart
(that would be durable-execution territory); persisted message history does.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
from pydantic_ai.exceptions import AgentRunError

from .engine import ChatBackendError


class Turn:
    def __init__(self, turn_id: str, session_id: str):
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

    def start(self, *, engine, store, session_id: str, prompt: str) -> str:
        turn_id = uuid.uuid4().hex[:12]
        turn = Turn(turn_id, session_id)
        self._turns[turn_id] = turn
        history = store.load_messages(session_id)
        store.set_active_turn(session_id, turn_id)
        task = asyncio.create_task(
            self._run(turn, engine, store, session_id, prompt, history)
        )
        self._tasks[turn_id] = task
        return turn_id

    async def _run(self, turn, engine, store, session_id, prompt, history) -> None:
        try:
            resume = store.resume_token(session_id)
            messages, resume_token = await engine.stream_turn(
                prompt, message_history=history, emit=turn.emit, resume=resume
            )
            store.save_messages(session_id, messages)
            if resume_token:
                # Carry the CLI session forward so the next turn resumes this
                # conversation (the demo backend returns None — nothing to carry).
                store.set_resume_token(session_id, resume_token)
        except (ChatBackendError, AgentRunError, httpx.HTTPError, OSError) as exc:
            # Expected failure modes of a model turn — backend unavailable
            # (ChatBackendError), any pydantic-ai model/API error (AgentRunError),
            # a network error (httpx), or a socket/subprocess error (OSError) —
            # reach the client as an error event. A genuine bug (KeyError,
            # ValueError, …) is NOT caught here: it propagates and surfaces
            # loudly rather than masquerading as a handled model failure. The
            # `finally` still emits `done`, so the client is never left hanging.
            turn.emit({"kind": "error", "text": f"{type(exc).__name__}: {exc}"})
        finally:
            store.set_active_turn(session_id, None)
            turn.emit({"kind": "done"})
            turn.finish()

    async def stream(self, turn_id: str, from_index: int = 0):
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
