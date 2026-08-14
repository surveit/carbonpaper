"""Session-opening factory for the generic chat engine: a host route opens a session
bound to a registered agent and redirects to its chat page (app.web.chat_router),
where every turn runs on the engine `build_session_engine` returns.
"""
from __future__ import annotations

from app.core.agent.registry import build_engine, render_opening_message
from app.core.agent.sdk_engine import ClaudeAgentSdkEngine
from app.core.agent.store import open_session_store, read_opening_message

_store = open_session_store()


def create_agent_session(
    agent_id: str, context: dict, *, base_url: str, title: str | None = None
) -> str:
    """An agent that opens with a message has it stored here, before any turn runs."""
    sid = _store.create(title=title or f"Agent: {agent_id}", agent_id=agent_id, context=context)
    opening = render_opening_message(agent_id, _turn_context(context, base_url))
    if opening:
        _store.append_messages(
            sid, [{"role": "assistant", "parts": [{"type": "text", "text": opening}]}]
        )
    return sid


def build_session_engine(sid: str, base_url: str) -> ClaudeAgentSdkEngine:
    """The opening message comes off the stored transcript, so page and model read the same words."""
    data = _store.load(sid)
    return build_engine(
        data["agent_id"],
        _turn_context(data.get("context") or {}, base_url),
        opening_message=read_opening_message(data.get("messages") or []),
    )


def _turn_context(context: dict, base_url: str) -> dict:
    """The stored context plus the address THIS reader is on, not the one it opened on."""
    return context | {"base_url": base_url}
