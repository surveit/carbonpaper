from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.agent.store import ChatBackend, open_session_store
from app.core.persistence import get_store
from app.main import app
from app.web import chat_router
from app.web.admin import workspace_router


client = TestClient(app)


def test_a_new_store_materializes_the_existing_claude_chat_default() -> None:
    from app.core.agent.chat_defaults import read_default_chat_backend

    assert read_default_chat_backend() == ChatBackend.claude
    assert get_store().list_ids("chat_default")


def test_a_missing_setting_after_initialization_refuses_new_sessions() -> None:
    from app.core.agent.chat_defaults import read_default_chat_backend

    read_default_chat_backend()
    [setting_id] = get_store().list_ids("chat_default")
    get_store().delete("chat_default", setting_id)

    with pytest.raises(RuntimeError, match="chat default setting is missing"):
        read_default_chat_backend()


def test_a_corrupt_setting_refuses_new_sessions() -> None:
    from app.core.agent.chat_defaults import read_default_chat_backend

    read_default_chat_backend()
    [setting_id] = get_store().list_ids("chat_default")
    get_store().write("chat_default", setting_id, {"id": setting_id, "backend": "other"})

    with pytest.raises(ValidationError):
        read_default_chat_backend()


def test_admin_shows_the_persisted_default_and_refuses_an_unavailable_change(monkeypatch) -> None:
    from app.core.agent.chat_defaults import set_default_chat_backend

    set_default_chat_backend(ChatBackend.codex)
    monkeypatch.setattr(
        workspace_router,
        "find_chat_backend_error",
        lambda _backend: "Codex is not authenticated.",
    )

    page = client.get("/admin")
    refused = client.post("/admin/chat-default", data={"backend": "codex"})

    assert 'option value="codex" selected' in page.text
    assert "Codex is not authenticated." in page.text
    assert refused.status_code == 409


def test_admin_persists_an_available_chat_default(monkeypatch) -> None:
    from app.core.agent.chat_defaults import read_default_chat_backend

    monkeypatch.setattr(workspace_router, "find_chat_backend_error", lambda _backend: None)

    response = client.post("/admin/chat-default", data={"backend": "claude"}, follow_redirects=False)

    assert response.status_code == 303
    assert read_default_chat_backend() == ChatBackend.claude


def test_chat_index_preselects_the_admin_default_but_posted_choice_wins(monkeypatch) -> None:
    from app.core.agent.chat_defaults import set_default_chat_backend

    set_default_chat_backend(ChatBackend.codex)
    monkeypatch.setattr(
        chat_router,
        "available_chat_backends",
        lambda: [ChatBackend.claude, ChatBackend.codex],
    )
    monkeypatch.setattr(chat_router, "find_codex_backend_error", lambda: None)

    page = client.get("/chat")
    response = client.post("/chat/new", data={"backend": "claude"}, follow_redirects=False)
    sid = response.headers["location"].rsplit("/", 1)[-1]

    assert 'option value="codex" selected' in page.text
    assert open_session_store().load(sid)["backend"] == "claude"


def test_a_draft_chat_locks_the_admin_default_when_it_materializes(monkeypatch) -> None:
    from app.core.agent.chat_defaults import set_default_chat_backend

    set_default_chat_backend(ChatBackend.codex)
    monkeypatch.setattr(chat_router, "find_codex_backend_error", lambda: None)

    response = client.post("/chat/agent/editing/sessions", json={"context": {}})
    sid = response.json()["sid"]

    assert open_session_store().load(sid)["backend"] == "codex"


def test_a_draft_chat_refuses_an_unavailable_persisted_default(monkeypatch) -> None:
    from app.core.agent.chat_defaults import set_default_chat_backend

    set_default_chat_backend(ChatBackend.codex)
    monkeypatch.setattr(chat_router, "find_codex_backend_error", lambda: RuntimeError("Sign in."))

    response = client.post("/chat/agent/editing/sessions", json={"context": {}})

    assert response.status_code == 409
    assert response.json()["detail"] == "Sign in."
