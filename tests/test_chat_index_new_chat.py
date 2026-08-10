"""The /chat index's New chat control: a chat opens bound to NO project, so the
control names none and needs none to exist. The agent asks which project it edits,
which `get_current_project` reports honestly as None until one is bound.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.agent.registry import build_engine
from app.core.agent.store import open_session_store
from app.main import app
from app.core.agent.sdk_engine import ClaudeAgentSdkEngine
from app.services.project import create_project, list_projects
from app.tools.editing import EditingContext, make_editing_tools

client = TestClient(app)
_store = open_session_store()


def read_current_project(sid: str) -> str | None:
    """What the session's own stored context binds `get_current_project` to."""
    context = _store.load(sid).get("context") or {}
    tools = make_editing_tools(EditingContext.model_validate(context))
    return next(t for t in tools if t.name == "get_current_project").fn()


def open_session(url: str) -> str:
    response = client.post(url, follow_redirects=False)
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/chat/")
    return location.removeprefix("/chat/")


def test_the_index_offers_a_new_chat_without_naming_a_project() -> None:
    create_project("trail", "Follow the filings.", source="test")
    body = client.get("/chat").text
    assert 'action="/chat/new"' in body
    assert "New chat" in body
    assert "<select" not in body
    assert "/edit-agent" not in body


def test_the_offer_stands_with_no_projects_at_all() -> None:
    assert list_projects() == []
    body = client.get("/chat").text
    assert 'action="/chat/new"' in body
    assert "No projects yet" not in body


def test_posting_to_chat_new_lands_on_an_editing_session_bound_to_no_project() -> None:
    sid = open_session("/chat/new")
    data = _store.load(sid)
    assert data["agent_id"] == "editing"
    assert data["context"].get("project_id") is None
    assert client.get(f"/chat/{sid}").status_code == 200


def test_a_projectless_session_reports_no_current_project() -> None:
    assert read_current_project(open_session("/chat/new")) is None


def test_a_session_opened_from_a_project_still_reports_that_project() -> None:
    name = create_project("trail", "Follow the filings.", source="test")
    assert read_current_project(open_session(f"/project/{name}/edit-agent")) == name


def test_the_editing_engine_builds_for_a_projectless_session() -> None:
    """build_engine validates the context and binds every tool, so not raising is the check."""
    data = _store.load(open_session("/chat/new"))
    assert isinstance(build_engine(data["agent_id"], data["context"]), ClaudeAgentSdkEngine)
