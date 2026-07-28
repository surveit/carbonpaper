"""Chat session store: an `AgentSession` record holding one engine-agnostic
transcript (``{role, parts}`` messages) plus the agent's resume token.

In-flight turns live in memory (app.core.agent.turns); surviving a server
restart mid-turn is out of scope.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, ClassVar

from pydantic import Field

from app.core.persistence import PersistedModel, PersistenceScope


class MessageRole(str, Enum):
    """A neutral-transcript message's `role` (see the module docstring)."""
    user = "user"
    assistant = "assistant"


class PartType(str, Enum):
    """A neutral-transcript message part's `type` (see the module docstring)."""
    text = "text"
    thinking = "thinking"
    tool_call = "tool_call"
    tool_result = "tool_result"


class AgentSession(PersistedModel):
    """A chat session: metadata, the bound agent + its context, and the stored
    transcript. `id` (inherited from PersistedModel) is the session id."""

    collection: ClassVar[str] = "agent_session"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    title: str = "New chat"
    agent_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)  # engine-neutral {role, parts} transcript
    active_turn: str | None = None
    pending_user: str | None = None
    sdk_session_id: str | None = None  # resume token (CLI session to resume)


def open_session_store() -> SessionStore:
    """The canonical session store the chat UI reads/writes. Both the chat routes
    and headless writers (e.g. generation) use this so their sessions land in the
    same document store and list together."""
    return SessionStore()


class SessionStore:
    """Stateless adapter over `AgentSession`: every method loads, mutates, and
    saves a record through the process-wide document store (app.core.persistence)
    — the store instance itself holds no state of its own."""

    def create(
        self,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> str:
        """Create a session bound to `agent_id` (which agent answers) carrying an
        opaque `context` (what that agent needs to bind its tools). Both are read
        back by the message route to build the engine for each turn."""
        sid = uuid.uuid4().hex[:12]
        AgentSession(
            id=sid,
            title=title or "New chat",
            agent_id=agent_id,
            context=context or {},
        ).save()
        return sid

    def exists(self, sid: str) -> bool:
        return AgentSession.exists(sid)

    def load(self, sid: str) -> dict:
        session = AgentSession.load(sid)
        return {"session_id": session.id, **session.model_dump(exclude={"id"})}

    def load_messages(self, sid: str) -> list[dict[str, Any]]:
        """Always empty: the agent's cross-turn memory comes from resuming the CLI
        session (see resume_token), not from replaying a transcript. Kept so the
        turn manager can pass a uniform ``message_history`` the engine ignores."""
        del sid
        return []

    def save_messages(self, sid: str, messages: list[dict[str, Any]]) -> None:
        """Persist the engine's neutral ``{role, parts}`` transcript verbatim — it
        is already plain JSON."""
        session = AgentSession.load(sid)
        session.messages = messages
        session.pending_user = None
        session.save()

    def set_active_turn(self, sid: str, turn_id: str | None) -> None:
        session = AgentSession.load(sid)
        session.active_turn = turn_id
        session.save()

    def resume_token(self, sid: str) -> str | None:
        """The CLI session id to resume for this chat session's next turn, or None
        on the first turn. Carries conversation memory across turns."""
        return AgentSession.load(sid).sdk_session_id

    def set_resume_token(self, sid: str, token: str) -> None:
        session = AgentSession.load(sid)
        session.sdk_session_id = token
        session.save()

    def set_pending_user(self, sid: str, text: str | None) -> None:
        session = AgentSession.load(sid)
        session.pending_user = text
        session.save()

    def list_sessions(self) -> list[dict]:
        newest_first = sorted(
            AgentSession.list(), key=lambda s: (s.created_at, s.id), reverse=True
        )
        return [
            {"session_id": s.id, "title": s.title, "created_at": s.created_at}
            for s in newest_first
        ]

    def history_view(self, sid: str) -> list[dict]:
        """The stored transcript rendered as simple bubbles for the template."""
        return _render_history_bubbles(AgentSession.load(sid).messages)


def _render_history_bubbles(messages: list[dict]) -> list[dict]:
    """Render a session's neutral transcript (``{role, parts}`` with part types
    ``text|thinking|tool_call|tool_result``) into bubble dicts ``chat.html``
    renders.

    The template's history loop only reads ``role``, ``text``, ``thinking`` and
    ``tools[].name/.args`` — tool results have no history slot and are not
    rendered on reload.
    """
    bubbles: list[dict] = []
    for message in messages:
        role = message.get("role")
        parts = message.get("parts") or []
        if role == MessageRole.user:
            text = "".join(p.get("text", "") for p in parts if p.get("type") == PartType.text)
            bubbles.append({"role": "user", "text": text})
        elif role == MessageRole.assistant:
            thinking = "".join(p.get("text", "") for p in parts if p.get("type") == PartType.thinking)
            text = "".join(p.get("text", "") for p in parts if p.get("type") == PartType.text)
            tools = [{"name": p.get("name", ""), "args": p.get("args", ""),
                      "label": p.get("label") or p.get("name", "")}
                     for p in parts if p.get("type") == PartType.tool_call]
            bubbles.append({"role": "assistant", "thinking": thinking,
                            "text": text, "tools": tools})
    return bubbles
