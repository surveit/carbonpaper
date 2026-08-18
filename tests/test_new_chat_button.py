"""The ＋ New chat button is on BOTH chat surfaces — the index and a conversation — so
leaving the one you are in is never the way to start another. It is a plain link to a
draft (app.web.chat_router.draft_agent_chat): nothing is created by rendering it."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.agent.session import create_agent_session
from app.main import app

client = TestClient(app)

NEW_CHAT_LINK = '<a class="ac-new btn primary" href="/chat/agent/editing/new">'


@pytest.fixture
def session_id() -> str:
    return create_agent_session(
        "editing", {}, base_url="http://testserver/", title="t")


def test_the_index_offers_a_new_chat(session_id):
    assert NEW_CHAT_LINK in client.get("/chat").text


def test_a_conversation_offers_one_too(session_id):
    assert NEW_CHAT_LINK in client.get(f"/chat/{session_id}").text


def test_the_button_links_to_a_draft_distinct_from_the_conversation_it_is_on(session_id):
    page = client.get(f"/chat/{session_id}").text

    assert NEW_CHAT_LINK in page
    assert f"/chat/{session_id}" not in page[page.index(NEW_CHAT_LINK):][:len(NEW_CHAT_LINK) + 40]
    assert client.get("/chat/agent/editing/new").status_code == 200
