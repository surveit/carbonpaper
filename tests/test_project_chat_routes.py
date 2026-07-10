"""Tests for the project-scoped chat routes: a session opened from a project
page runs on that project's editing agent (bound tools + prompt), not the
generic demo engine.

CW_CHAT_BACKEND=dev is set before every request so get_project_agent builds on
the scripted dev model — no API key, no real LLM, no network. The project name
used here ("alpha") need not exist on disk: build_project_agent binds tool
closures without requiring the project directory to exist; only tools that
actually touch the filesystem (describe_workflow, read_stage, ...) would fail,
and the dev model here never reaches that turn's tool-argument stage in a way
that requires alpha to exist (list_projects, the first tool, takes no args and
tolerates a missing examples dir)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_new_project_session_redirects_to_chat_page(monkeypatch) -> None:
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    r = client.post("/chat/project/alpha/sessions", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat/")


def test_new_project_session_records_project_in_context(monkeypatch) -> None:
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    r = client.post("/chat/project/alpha/sessions", follow_redirects=False)
    sid = r.headers["location"].rsplit("/", 1)[-1]
    data = client.get(f"/chat/{sid}/messages").json()
    assert data["context"] == {"project": "alpha"}
    assert data["title"] == "Editing: alpha"


def test_post_project_message_starts_a_turn(monkeypatch) -> None:
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    sid_resp = client.post("/chat/project/alpha/sessions", follow_redirects=False)
    sid = sid_resp.headers["location"].rsplit("/", 1)[-1]

    r = client.post(f"/chat/{sid}/project/alpha/message", json={"text": "what stages exist?"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["turn_id"]


def test_post_project_message_missing_session_is_404(monkeypatch) -> None:
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    r = client.post("/chat/doesnotexist/project/alpha/message", json={"text": "hi"})
    assert r.status_code == 404


def test_post_project_message_empty_text_is_400(monkeypatch) -> None:
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    sid_resp = client.post("/chat/project/alpha/sessions", follow_redirects=False)
    sid = sid_resp.headers["location"].rsplit("/", 1)[-1]
    r = client.post(f"/chat/{sid}/project/alpha/message", json={"text": "   "})
    assert r.status_code == 400


def test_chat_page_for_a_project_session_names_the_project_for_the_composer(monkeypatch) -> None:
    """The page a project session redirects to must tell its JS composer which
    project it belongs to, so it posts to the PROJECT message route — otherwise
    the editing agent is never actually reached by the 'Edit with agent' button
    (D3). chat.html builds the fetch URL from a JS const (see PROJECT below),
    not a server-interpolated sid, so we assert on that const's rendered value."""
    monkeypatch.setenv("CW_CHAT_BACKEND", "dev")
    sid_resp = client.post("/chat/project/alpha/sessions", follow_redirects=False)
    sid = sid_resp.headers["location"].rsplit("/", 1)[-1]

    page = client.get(f"/chat/{sid}")
    assert page.status_code == 200
    assert 'const PROJECT = "alpha";' in page.text
