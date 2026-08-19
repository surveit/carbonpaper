from __future__ import annotations

from typing import cast

import pytest

import app.agents.compiler.config  # noqa: F401 — registers the editing agent
from app.core.agent.codex_engine import CodexChatEngine
from app.core.agent.session import build_session_engine, create_agent_session
from app.core.agent.store import AgentSession, ChatBackend, open_session_store


def test_new_session_persists_its_selected_backend() -> None:
    sid = create_agent_session(
        "editing", {}, base_url="http://reader/", backend=ChatBackend.codex
    )

    assert open_session_store().load(sid)["backend"] == "codex"


def test_legacy_session_defaults_to_claude() -> None:
    AgentSession(id="legacy", agent_id="editing", context={}).save()

    assert open_session_store().load("legacy")["backend"] == "claude"


def test_unknown_backend_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown chat backend"):
        build_session_engine("editing", {}, cast(ChatBackend, "other"))


def test_codex_backend_builds_the_codex_engine_without_an_mcp_server() -> None:
    engine = build_session_engine(
        "editing",
        {"project_id": None, "base_url": "http://reader/"},
        ChatBackend.codex,
    )

    assert isinstance(engine, CodexChatEngine)
    assert engine._command == ("codex", "app-server", "--stdio")
    assert not hasattr(engine, "_mcp_server")
