"""The data-model generation phase persists its agent conversation as a viewable chat
session — on success AND on failure — so a completed OR failed generation leaves a
session the chat UI can open, instead of silence.

The agent and the session store are faked; no CLI subprocess, no real LLM.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import app.services.generation as generation
from app.agent.store import SessionStore
from app.errors import GenerationError


class _FakeLibrary:
    """Stands in for a SchemaLibrary: _persist_schemas only reads `.schemas`."""

    schemas: list[Any] = []


class _FakeAgent:
    """Stands in for app.agent.Agent: run() returns the library or raises, and the
    transcript is available either way (as the real Agent captures it before raising)."""

    def __init__(self, *, transcript: list[dict[str, Any]], library: Any = None,
                 error: Exception | None = None) -> None:
        self.transcript = transcript
        self.session_id = "sess-x"
        self._library = library
        self._error = error

    async def run(self) -> Any:
        if self._error is not None:
            raise self._error
        return self._library


def _wire(monkeypatch: Any, tmp_path: Path, agent: _FakeAgent) -> SessionStore:
    monkeypatch.setattr(generation, "build_data_model_agent", lambda *a, **k: agent)
    store = SessionStore(tmp_path / "sessions")
    monkeypatch.setattr(generation, "open_session_store", lambda: store)
    return store


def test_data_model_phase_persists_the_conversation_on_success(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    transcript = [
        {"role": "user", "parts": [{"type": "text", "text": "author the data model"}]},
        {"role": "assistant", "parts": [
            {"type": "tool_call", "name": "submit_answer", "args": '{"schemas": []}'},
        ]},
    ]
    store = _wire(monkeypatch, tmp_path, _FakeAgent(transcript=transcript, library=_FakeLibrary()))

    ok = generation._generate_data_model(project_dir, "the document", "sonnet")

    assert ok is True
    sessions = store.list_sessions()
    assert len(sessions) == 1
    sid = sessions[0]["session_id"]
    assert any("submit_answer" in str(bubble) for bubble in store.history_view(sid))


def test_data_model_phase_persists_the_conversation_on_failure(tmp_path: Path, monkeypatch: Any):
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    transcript = [
        {"role": "user", "parts": [{"type": "text", "text": "author the data model"}]},
    ]
    agent = _FakeAgent(transcript=transcript, error=GenerationError("no valid SchemaLibrary"))
    store = _wire(monkeypatch, tmp_path, agent)

    ok = generation._generate_data_model(project_dir, "the document", "sonnet")

    assert ok is False
    # A session STILL exists, and it surfaces the failure rather than looking unfinished.
    sessions = store.list_sessions()
    assert len(sessions) == 1
    sid = sessions[0]["session_id"]
    view = store.history_view(sid)
    assert any("failed" in str(bubble).lower() for bubble in view)
