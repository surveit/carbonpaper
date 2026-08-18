"""Session-opening factory for the generic chat engine: a host route opens a session
bound to a registered agent and redirects to its chat page (app.web.chat_router),
where every turn runs on the engine `build_session_engine` returns.
"""
from __future__ import annotations

from typing import overload

from app.core.agent.registry import (
    ChatEngine,
    build_session_engine as build_registered_session_engine,
    render_opening_message,
)
from app.core.agent.store import (
    AgentContext,
    ChatBackend,
    open_session_store,
    read_opening_message,
)

_store = open_session_store()


def create_agent_session(
    agent_id: str,
    context: AgentContext,
    *,
    base_url: str,
    title: str | None = None,
    backend: ChatBackend = ChatBackend.claude,
) -> str:
    """An agent that opens with a message has it stored here, before any turn runs."""
    sid = _store.create(
        title=title or f"Agent: {agent_id}",
        agent_id=agent_id,
        backend=backend,
        context=context,
    )
    opening = render_opening_message(agent_id, _turn_context(context, base_url))
    if opening:
        _store.append_messages(
            sid, [{"role": "assistant", "parts": [{"type": "text", "text": opening}]}]
        )
    return sid


@overload
def build_session_engine(
    agent_id_or_sid: str,
    context_or_base_url: AgentContext,
    backend: ChatBackend,
) -> ChatEngine: ...


@overload
def build_session_engine(agent_id_or_sid: str, context_or_base_url: str) -> ChatEngine: ...


def build_session_engine(
    agent_id_or_sid: str,
    context_or_base_url: AgentContext | str,
    backend: ChatBackend = ChatBackend.claude,
) -> ChatEngine:
    if isinstance(context_or_base_url, dict):
        return build_registered_session_engine(agent_id_or_sid, context_or_base_url, backend)
    return _build_stored_session_engine(agent_id_or_sid, context_or_base_url)


def _build_stored_session_engine(sid: str, base_url: str) -> ChatEngine:
    data = _store.load(sid)
    return build_registered_session_engine(
        data["agent_id"],
        _turn_context(data.get("context") or {}, base_url),
        ChatBackend(data["backend"]),
        opening_message=read_opening_message(data.get("messages") or []),
    )


def _turn_context(context: AgentContext, base_url: str) -> AgentContext:
    """The stored context plus the address THIS reader is on, not the one it opened on."""
    return context | {"base_url": base_url}
