"""How a generation turn records a failure a client that was not watching it would
otherwise never see: an assistant message appended to the session's stored transcript,
which the generation-session status route reports."""
from __future__ import annotations

from app.core.agent.store import SessionStore

# The marker the status route matches on to report a failed generation turn
# (app/web/routers/node_review.py, generation_session_status).
DERIVATION_FAILURE_PREFIX = "derivation failed: "


def persist_derivation_failure(
    store: SessionStore, session_id: str, error: Exception
) -> None:
    messages = list(store.load(session_id)["messages"])
    messages.append({
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{DERIVATION_FAILURE_PREFIX}{error}"}],
    })
    store.save_messages(session_id, messages)
