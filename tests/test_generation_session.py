"""Generation's data-model phase runs as a LIVE chat turn: start_generation creates a
session and starts the data-model agent as a turn on the shared TurnManager (streamable at
/chat/<sid> while it runs); when the turn ends with a valid submission, the schemas are
written and the workflow phase is kicked.

The agent + turn are faked; no CLI subprocess, no real LLM. Driven with asyncio.run.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import app.services.generation as generation
from app.agent.store import SessionStore
from app.agent.turns import TurnManager


class _FakeLibrary:
    """Stands in for a SchemaLibrary: _persist_schemas only reads `.schemas`."""

    schemas: list[Any] = []


# ── the completion hook (_finish_data_model): the substantive branch logic ──────────

def test_finish_persists_schemas_and_kicks_workflow_on_success(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    kicked: list[Any] = []
    monkeypatch.setattr(generation, "_start_workflow", lambda *a: kicked.append(a))

    generation._finish_data_model(project_dir, "the document", "sonnet", _FakeLibrary())

    assert (project_dir / "schemas").exists()  # schemas persisted
    assert kicked  # workflow phase kicked


def test_finish_does_nothing_when_no_answer_was_submitted(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    kicked: list[Any] = []
    monkeypatch.setattr(generation, "_start_workflow", lambda *a: kicked.append(a))

    generation._finish_data_model(project_dir, "the document", "sonnet", None)

    assert not (project_dir / "schemas").exists()  # nothing built on a failed data model
    assert not kicked


# ── start_generation wiring: session up front + a live, streamable turn ──────────────

class _FakeAgent:
    """Stands in for the data-model Agent driven as a live turn: build_engine() returns
    an engine whose stream_turn 'submits' an answer (sets `_answer`) and returns a
    transcript, exactly as the real submit_answer + engine would during the turn."""

    task = "author the data model and submit it"

    def __init__(self) -> None:
        self._answer: Any = None

    @property
    def answer(self) -> Any:
        return self._answer

    def build_engine(self) -> Any:
        agent = self

        class _Engine:
            async def stream_turn(self, prompt: str, *, message_history: Any, emit: Any, resume: Any):
                emit({"kind": "text", "text": "authored"})
                agent._answer = _FakeLibrary()  # the submit_answer tool would set this
                return [{"role": "assistant", "parts": [{"type": "text", "text": "authored"}]}], None

        return _Engine()


def test_start_generation_creates_a_session_and_runs_a_live_turn(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    store = SessionStore(tmp_path / "sessions")
    turns = TurnManager()
    monkeypatch.setattr(generation, "open_session_store", lambda: store)
    monkeypatch.setattr(generation, "default_turn_manager", lambda: turns)
    monkeypatch.setattr(generation, "build_data_model_agent", lambda *a, **k: _FakeAgent())
    monkeypatch.setattr(generation, "_start_workflow", lambda *a: None)

    async def _drive() -> str:
        sid = generation.start_generation(project_dir, document="doc", model="sonnet")
        # The originating prompt is shown as the user's message (pending_user) so the LIVE
        # view doesn't lose it — checked before the turn completes and clears it.
        assert store.load(sid)["pending_user"] == _FakeAgent.task
        turn_id = store.load(sid)["active_turn"]
        assert turn_id, "a live turn should be active on the session while it generates"
        await turns._tasks[turn_id]
        return sid

    sid = asyncio.run(_drive())

    assert store.exists(sid)
    assert store.load(sid)["messages"]          # TurnManager persisted the conversation
    assert (project_dir / "schemas").exists()   # completion hook persisted the schemas
