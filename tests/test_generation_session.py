from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import app.compiler.data_model as data_model
import app.services.generation as generation
from app.core.agent.store import SessionStore
from app.core.agent.turns import TurnManager
from app.models.named_schemas import NamedSchema, SchemaKind, SchemaLibrary
from app.services.data_model import load_schemas

_A_SCHEMA = NamedSchema(name="claim", kind=SchemaKind.reference, title="Claim", columns=[])


# ── the completion hook (_finish_data_model): persist the schemas, nothing more ──────

def test_finish_persists_schemas_on_success(tmp_path: Path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    generation._finish_data_model(
        project_dir, SchemaLibrary(schemas=[_A_SCHEMA]))

    # Persisted; the workflow is NOT auto-built.
    assert load_schemas("demo") == [_A_SCHEMA]


def test_finish_does_nothing_when_no_answer_was_submitted(tmp_path: Path):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()

    generation._finish_data_model(project_dir, None)

    assert load_schemas("demo") == []  # nothing written on a failed data model


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
                agent._answer = SchemaLibrary(schemas=[_A_SCHEMA])  # submit_answer would set this
                return [{"role": "assistant", "parts": [{"type": "text", "text": "authored"}]}], None

        return _Engine()


def test_start_generation_creates_a_session_and_runs_a_live_turn(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    store = SessionStore()
    turns = TurnManager()
    # The app.core.agent bridge (session + live turn) lives in app.compiler.data_model, which
    # generation delegates to.
    monkeypatch.setattr(data_model, "open_session_store", lambda: store)
    monkeypatch.setattr(data_model, "default_turn_manager", lambda: turns)
    monkeypatch.setattr(data_model, "build_data_model_agent", lambda *a, **k: _FakeAgent())

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
    assert load_schemas("demo") == [_A_SCHEMA]  # completion hook persisted the schemas
