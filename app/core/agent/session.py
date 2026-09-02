"""Session-opening factory for the generic chat engine: a host route opens a session
bound to a registered agent and redirects to its chat page (app.web.chat_router),
where every turn runs on the engine `build_session_engine` returns.
"""
from __future__ import annotations

from typing import Any

from app.core.agent.registry import OpeningTurn, build_engine, render_opening_turn
from app.core.agent.sdk_engine import ClaudeAgentSdkEngine
from app.core.agent.store import open_session_store, read_opening_message
from app.core.ids import ID

_store = open_session_store()


def create_agent_session(
    agent_id: ID, context: dict, *, base_url: str, title: str | None = None
) -> str:
    """An agent that opens with a message has it stored here, before any turn runs."""
    session_id = _store.create(
        title=title or f"Agent: {agent_id}", agent_id=agent_id, context=context
    )
    opening = render_opening_turn(agent_id, _turn_context(context, base_url))
    if opening and opening.text:
        _store.append_messages(
            session_id, [{"role": "assistant", "parts": _opening_parts(opening)}]
        )
    return session_id


def _opening_parts(opening: OpeningTurn) -> list[dict[str, Any]]:
    """The offers ride the same turn, so the page draws them wherever it draws the words."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": opening.text}]
    if opening.offers:
        parts.append({"type": "offer", "options": opening.offers})
    return parts


def build_session_engine(
    session_id: ID, base_url: str, page: str | None = None
) -> ClaudeAgentSdkEngine:
    """The opening message comes off the stored transcript, so page and model read the same words."""
    data = _store.load(session_id)
    return build_engine(
        data["agent_id"],
        _turn_context(data.get("context") or {}, base_url, page, session_id),
        opening_message=read_opening_message(data.get("messages") or []),
    )


def _turn_context(
    context: dict, base_url: str, page: str | None = None, session_id: ID | None = None
) -> dict:
    """The stored context plus where THIS reader is now, not where the session opened."""
    return context | {"base_url": base_url, "page": page, "session_id": session_id}
