from __future__ import annotations

import subprocess

from fastapi.testclient import TestClient

from app.core.agent import codex_availability
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
    monkeypatch.setattr(chat_router, "find_codex_backend_error", lambda: None)

    response = client.post("/chat/new", data={"backend": "codex"}, follow_redirects=False)
    sid = response.headers["location"].rsplit("/", 1)[-1]

    assert open_session_store().load(sid)["backend"] == "codex"


def test_unavailable_codex_is_refused(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router,
        "find_codex_backend_error",
        lambda: codex_availability.CodexBackendUnavailableError(
            "Codex isn't authenticated with a ChatGPT subscription. Run `codex login` "
            "before starting a chat."
        ),
    )

    assert client.post("/chat/new", data={"backend": "codex"}).status_code == 409


def test_installed_signed_out_codex_is_not_offered_or_persisted(monkeypatch) -> None:
    monkeypatch.setattr(codex_availability.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(
        codex_availability.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], returncode=1),
    )

    assert ChatBackend.codex not in chat_router.available_chat_backends()
    response = client.post("/chat/new", data={"backend": "codex"})

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Codex isn't authenticated with a ChatGPT subscription. Run `codex login` "
        "before starting a chat."
    )


def test_authenticated_codex_is_offered(monkeypatch) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(codex_availability.shutil, "which", lambda _name: "codex")

    def complete_status(*args, **_kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], returncode=0)

    monkeypatch.setattr(codex_availability.subprocess, "run", complete_status)

    assert ChatBackend.codex in chat_router.available_chat_backends()
    assert calls == [(('codex', 'login', 'status'),)]


def test_missing_backend_is_refused() -> None:
    assert client.post("/chat/new", data={}).status_code == 422


def test_stored_codex_session_reports_its_unavailability(monkeypatch) -> None:
    monkeypatch.setattr(
        chat_router,
        "find_codex_backend_error",
        lambda: codex_availability.CodexBackendUnavailableError(
            "The Codex CLI isn't available. Install it before starting a chat."
        ),
    )
    sid = open_session_store().create(agent_id="editing", backend=ChatBackend.codex)

    response = client.get(f"/chat/{sid}")

    assert "The Codex CLI isn&#39;t available." in response.text
