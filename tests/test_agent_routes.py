"""Tests for the generic chat routes: a session is bound to a registered agent by
an agent_id + opaque context, and a message turn builds that agent's engine via
the registry. The routes know nothing about any specific agent, so these tests
register a throwaway agent rather than depending on the editing agent.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.web.chat_router import _store
from app.core.agent import registry
from app.core.agent.registry import AgentConfig, register
from app.core.agent.bound_tool import BoundToolSpec
from app.main import app

client = TestClient(app)


class _Ctx(BaseModel):
    label: str
    base_url: str = ""


_CONTEXTS_BUILT: list[_Ctx] = []


def _build_tools(ctx: BaseModel) -> list:
    assert isinstance(ctx, _Ctx)
    _CONTEXTS_BUILT.append(ctx)

    def echo() -> str:
        return ctx.label

    return [
        BoundToolSpec(
            name="echo",
            description="Echo the context label.",
            fn=echo,
            input_schema={},
            label="Echoing",
        )
    ]


@pytest.fixture(autouse=True)
def register_dummy_agent() -> Iterator[None]:
    register(
        "dummy",
        AgentConfig(system_prompt="sp", context_schema=_Ctx),
        _build_tools,
    )
    yield
    registry._registry.pop("dummy", None)


def _new_session(context: dict) -> str:
    r = client.post(
        "/chat/agent/dummy/sessions", json={"context": context}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/chat/")
    return r.headers["location"].rsplit("/", 1)[-1]


def test_new_agent_session_records_agent_and_context() -> None:
    sid = _new_session({"label": "hello"})
    data = client.get(f"/chat/{sid}/messages").json()
    assert data["agent_id"] == "dummy"
    assert data["context"] == {"label": "hello"}


def test_post_message_starts_a_turn() -> None:
    sid = _new_session({"label": "hello"})
    r = client.post(f"/chat/{sid}/message", json={"text": "hi there"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["turn_id"]


def test_a_turn_is_told_the_address_its_reader_is_on() -> None:
    # Not stored with the session: a reader on another address gets links for THEM.
    sid = _new_session({"label": "hello"})
    _CONTEXTS_BUILT.clear()

    client.post(f"/chat/{sid}/message", json={"text": "hi there"})

    assert [c.base_url for c in _CONTEXTS_BUILT] == ["http://testserver/"]
    assert client.get(f"/chat/{sid}/messages").json()["context"] == {"label": "hello"}


def test_post_message_missing_session_is_404() -> None:
    r = client.post("/chat/doesnotexist/message", json={"text": "hi"})
    assert r.status_code == 404


def test_post_message_empty_text_is_400() -> None:
    sid = _new_session({"label": "hello"})
    r = client.post(f"/chat/{sid}/message", json={"text": "   "})
    assert r.status_code == 400


def test_chat_page_renders_the_composer() -> None:
    sid = _new_session({"label": "hello"})
    page = client.get(f"/chat/{sid}")
    assert page.status_code == 200
    assert f'const SID = "{sid}";' in page.text
    assert "const MESSAGE_URL = `/chat/${SID}/message`;" in page.text
    assert 'id="input"' in page.text  # a bound agent keeps the message composer


def test_chat_page_hides_composer_for_view_only_session() -> None:
    # No bound agent (agent_id=None), so post_message would 400 — hence no composer.
    sid = _store.create(title="Generation", agent_id=None, context={"phase": "workflow"})
    page = client.get(f"/chat/{sid}")
    assert page.status_code == 200
    assert 'id="input"' not in page.text        # no message box on a view-only session
    assert "read-only" in page.text.lower()      # copy explains why
    assert "EventSource(" in page.text           # but it can still watch the live turn
