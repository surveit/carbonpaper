"""Tests for hidden sessions: sessions with context.get("hidden") is True must not
appear in the /chat index listing, but GET /chat/{sid} should still serve them."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.web.chat_router import _store
from app.main import app

client = TestClient(app)


def test_chat_index_excludes_hidden_sessions() -> None:
    """GET /chat index lists normal sessions but excludes those with hidden=True."""
    # Create one normal session and one hidden session.
    normal_sid = _store.create(title="Normal Session", agent_id=None, context={})
    hidden_sid = _store.create(title="Hidden Session", agent_id=None, context={"hidden": True})

    # Fetch the index.
    response = client.get("/chat")
    assert response.status_code == 200
    body = response.text

    # The normal session should appear in the index.
    assert normal_sid in body
    assert "Normal Session" in body

    # The hidden session should NOT appear in the index.
    assert hidden_sid not in body
    assert "Hidden Session" not in body


def test_direct_access_to_hidden_session_works() -> None:
    """GET /chat/{hidden_sid} should still serve hidden sessions."""
    hidden_sid = _store.create(title="Hidden Session", agent_id=None, context={"hidden": True})

    response = client.get(f"/chat/{hidden_sid}")
    assert response.status_code == 200
    assert f'const SID = "{hidden_sid}";' in response.text
    assert "Hidden Session" in response.text
