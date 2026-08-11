"""Chat session store: an `AgentSession` record holding one engine-agnostic
transcript (``{role, parts}`` messages) plus the agent's resume token.

In-flight turns live in memory (app.core.agent.turns); surviving a server
restart mid-turn is out of scope.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, ClassVar, TypedDict

from pydantic import Field

from app.core.persistence import PersistedModel, PersistenceScope


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"


class PartType(str, Enum):
    text = "text"
    thinking = "thinking"
    tool_call = "tool_call"
    tool_result = "tool_result"


class TranscriptMessage(TypedDict):
    """One stored transcript message; `role` is a MessageRole value."""

    role: str
    parts: list[dict[str, Any]]


class AgentSession(PersistedModel):
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
    return SessionStore()


class SessionStore:
    def create(
        self,
        *,
        title: str | None = None,
        agent_id: str | None = None,
        context: dict | None = None,
    ) -> str:
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
        """Always empty: cross-turn memory comes from the CLI resume token, not from a replayed transcript."""
        del sid
        return []

    def save_messages(self, sid: str, messages: list[dict[str, Any]]) -> None:
        session = AgentSession.load(sid)
        session.messages = messages
        session.pending_user = None
        session.save()

    def set_active_turn(self, sid: str, turn_id: str | None) -> None:
        session = AgentSession.load(sid)
        session.active_turn = turn_id
        session.save()

    def resume_token(self, sid: str) -> str | None:
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
        return _render_history_bubbles(AgentSession.load(sid).messages)

    def read_last_assistant_text(self, sid: str) -> str:
        """Empty when the newest turn produced no text (tools only), or stored nothing."""
        for bubble in reversed(self.history_view(sid)):
            if bubble["role"] == "assistant":
                return str(bubble["text"])
        return ""


def _render_history_bubbles(messages: list[dict]) -> list[dict]:
    """Tool results have no bubble: they are dropped here and never rendered on reload."""
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
