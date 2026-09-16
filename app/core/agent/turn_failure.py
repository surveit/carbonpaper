"""How a generation turn records a failure a client that was not watching it would
otherwise never see: an assistant message appended to the session's stored transcript,
which the generation-session status route reports."""
from __future__ import annotations

from app.core.agent.store import SessionStore

# The marker the status route matches on to report a failed generation turn
# (app/web/routers/node.py, generation_session_status).
GENERATION_FAILURE_PREFIX = "generation failed: "


def persist_generation_failure(
    store: SessionStore, session_id: str, error: Exception
) -> None:
    store.append_messages(session_id, [{
        "role": "assistant",
        "parts": [{"type": "text", "text": f"{GENERATION_FAILURE_PREFIX}{error}"}],
    }])
