"""The ＋ New chat button is on BOTH chat surfaces — the index and a conversation — so
leaving the one you are in is never the way to start another."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

NEW_CHAT_FORM = '<form class="ac-new" method="post" action="/chat/new">'


@pytest.fixture
def session_id() -> str:
    resp = client.post("/chat/new", follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_the_index_offers_a_new_chat(session_id):
    assert NEW_CHAT_FORM in client.get("/chat").text


def test_a_conversation_offers_one_too(session_id):
    assert NEW_CHAT_FORM in client.get(f"/chat/{session_id}").text


def test_the_button_opens_a_session_and_lands_on_it(session_id):
    resp = client.post("/chat/new", follow_redirects=False)
    assert resp.status_code == 303
    opened = resp.headers["location"]
    assert opened != f"/chat/{session_id}"  # a new one, not the one already open
    assert client.get(opened).status_code == 200
