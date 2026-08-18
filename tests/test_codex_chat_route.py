from __future__ import annotations

from fastapi.testclient import TestClient

from app.core.agent.store import ChatBackend, open_session_store
from app.main import app
from app.web import chat_router

client = TestClient(app)


def test_index_lists_codex_when_subscription_is_available(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router,
        "available_chat_backends",
        lambda: [ChatBackend.claude, ChatBackend.codex],
    )

    assert 'value="codex"' in client.get("/chat").text


def test_posting_codex_persists_the_backend(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router, "available_chat_backends", lambda: [ChatBackend.codex]
    )

    response = client.post("/chat/new", data={"backend": "codex"}, follow_redirects=False)
    sid = response.headers["location"].rsplit("/", 1)[-1]

    assert open_session_store().load(sid)["backend"] == "codex"


def test_unavailable_codex_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router, "available_chat_backends", lambda: [ChatBackend.claude]
    )

    assert client.post("/chat/new", data={"backend": "codex"}).status_code == 409


def test_missing_backend_is_refused() -> None:
    assert client.post("/chat/new", data={}).status_code == 422


def test_stored_codex_session_reports_its_unavailability(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router, "available_chat_backends", lambda: [ChatBackend.claude]
    )
    sid = open_session_store().create(agent_id="editing", backend=ChatBackend.codex)

    response = client.get(f"/chat/{sid}")

    assert "The Codex CLI isn&#39;t available." in response.text
