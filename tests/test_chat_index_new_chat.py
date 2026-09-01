"""The /chat index's New chat control: a chat opens bound to NO project, so the
control names none and needs none to exist. The agent asks which project it edits,
which the session note reports honestly as none until one is bound.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.agent.registry import build_engine
from app.core.agent.store import open_session_store
from app.main import app
from app.core.agent.sdk_engine import ClaudeAgentSdkEngine
from app.services.project import create_project, list_projects
from app.tools.editing import EditingContext

client = TestClient(app)
_store = open_session_store()


# The stored context carries no address — a turn adds the one its reader is on.
_READER = {"base_url": "http://testserver/"}


def read_current_project(sid: str) -> str | None:
    """What the session's own stored context binds the conversation to."""
    stored = (_store.load(sid).get("context") or {}) | _READER
    return EditingContext.model_validate(stored).project_id


def open_session(agent_id: str, context: dict | None = None) -> str:
    """What a draft page's first reply does — see ensureSession() in chat.html."""
    response = client.post(
        f"/chat/agent/{agent_id}/sessions", json={"context": context or {}})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"], data
    return data["sid"]


def test_the_index_offers_a_new_chat_without_naming_a_project() -> None:
    create_project("trail", "Follow the filings.", source="test").id
    body = client.get("/chat").text
    assert 'href="/chat/agent/editing/new"' in body
    assert "New chat" in body
    assert "<select" not in body
    assert "/edit-agent" not in body


def test_the_offer_stands_with_no_projects_at_all() -> None:
    assert list_projects() == []
    body = client.get("/chat").text
    assert 'href="/chat/agent/editing/new"' in body
    assert "No projects yet" not in body


def test_materializing_a_bare_context_lands_on_an_editing_session_bound_to_no_project() -> None:
    sid = open_session("editing")
    data = _store.load(sid)
    assert data["agent_id"] == "editing"
    assert data["context"].get("project_id") is None
    assert client.get(f"/chat/{sid}").status_code == 200


def test_a_projectless_session_reports_no_current_project() -> None:
    assert read_current_project(open_session("editing")) is None


def test_a_session_materialized_with_a_project_still_reports_that_project() -> None:
    name = create_project("trail", "Follow the filings.", source="test").id
    assert read_current_project(open_session("editing", {"project_id": name})) == name


def test_the_editing_engine_builds_for_a_projectless_session() -> None:
    """build_engine validates the context and binds every tool, so not raising is the check."""
    data = _store.load(open_session("editing"))
    engine = build_engine(data["agent_id"], data["context"] | _READER)
    assert isinstance(engine, ClaudeAgentSdkEngine)
