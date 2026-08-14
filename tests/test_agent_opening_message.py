"""AgentConfig.render_opening_message — the agent's first message is written, not
generated. It is stored at session creation, so the page renders it out of history,
and it is appended to the system prompt, so the model knows what it opened with.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.core.agent import registry
from app.core.agent.registry import AgentConfig, register
from app.core.agent.session import build_session_engine, create_agent_session
from app.core.agent.store import open_session_store
from app.main import app

client = TestClient(app)
_store = open_session_store()

_BASE_URL = "http://testserver/"
_OPENING = "Three ways in. 1. Bring data. 2. Bring a methodology. 3. Change a project."


class _Ctx(BaseModel):
    label: str = "anything"


def _no_tools(context: BaseModel) -> list:
    del context
    return []


def _open_with(context: BaseModel) -> str:
    del context
    return _OPENING


@pytest.fixture(autouse=True)
def throwaway_agents() -> Iterator[None]:
    """Registered per test rather than leaning on editing/tutorial copy, which owns its own wording."""
    register("silent", AgentConfig(system_prompt="sp", context_schema=_Ctx), _no_tools)
    register(
        "greeter",
        AgentConfig(system_prompt="sp", context_schema=_Ctx, render_opening_message=_open_with),
        _no_tools,
    )
    yield
    for agent_id in ("silent", "greeter"):
        registry._registry.pop(agent_id, None)


def _open_session(agent_id: str) -> str:
    return create_agent_session(agent_id, {}, base_url=_BASE_URL, title="t")


def test_an_agent_with_no_opening_message_stores_nothing() -> None:
    assert _store.load(_open_session("silent"))["messages"] == []


def test_the_written_message_is_stored_as_the_agents_first_turn() -> None:
    messages = _store.load(_open_session("greeter"))["messages"]

    assert messages == [
        {"role": "assistant", "parts": [{"type": "text", "text": _OPENING}]}
    ]


def test_the_model_is_told_what_this_conversation_opened_with() -> None:
    """The engine drops message_history and the store replays none, so the system prompt is the only route."""
    engine = build_session_engine(_open_session("greeter"), _BASE_URL)

    assert _OPENING in engine._system_prompt


def test_a_session_that_opened_with_nothing_says_so_to_nobody() -> None:
    assert build_session_engine(_open_session("silent"), _BASE_URL)._system_prompt == "sp"


def test_the_page_renders_the_opening_message_and_asks_nothing_of_the_server() -> None:
    """It is history like any other message: no opening turn, and no route to start one."""
    sid = _open_session("greeter")

    page = client.get(f"/chat/{sid}")

    assert _OPENING in page.text
    assert "/open" not in page.text
    assert client.post(f"/chat/{sid}/open").status_code == 404
