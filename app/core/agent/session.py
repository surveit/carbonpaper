"""Session-opening factory for the generic chat engine.

A host route (e.g. the compiler's 'Edit with agent' button) calls
`create_agent_session` to open a session bound to a registered agent, then
redirects the browser to that session's chat page (app.web.chat_router).
"""
from __future__ import annotations

from app.core.agent.registry import canned_opening
from app.core.agent.store import open_session_store

_store = open_session_store()


def create_agent_session(agent_id: str, context: dict, *, title: str | None = None) -> str:
    sid = _store.create(title=title or f"Agent: {agent_id}", agent_id=agent_id, context=context)
    opening = canned_opening(agent_id)
    if opening is not None:
        _store.append_messages(sid, [{
            "role": "assistant",
            "parts": [{"type": "text", "text": opening}],
        }])
    return sid
