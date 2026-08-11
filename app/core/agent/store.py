"""Chat session store: an `AgentSession` record holding one engine-agnostic
transcript (``{role, parts}`` messages) plus the agent's resume token.

In-flight turns live in memory (app.core.agent.turns); surviving a server
restart mid-turn is out of scope.
"""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Any, ClassVar, Literal, TypedDict

from pydantic import BaseModel, Field

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


class ProseBlock(BaseModel):
    kind: Literal["text", "thinking"]
    text: str


class ToolBlock(BaseModel):
    kind: Literal["tool"] = "tool"
    name: str
    args: str
    label: str


class Bubble(BaseModel):
    role: MessageRole
    blocks: list[ProseBlock | ToolBlock]


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

    def history_view(self, sid: str) -> list[Bubble]:
        return _render_history_bubbles(AgentSession.load(sid).messages)

    def read_last_reply_texts(self, sid: str) -> list[str]:
        """The newest reply's text blocks in turn order; empty when it only called tools."""
        for bubble in reversed(self.history_view(sid)):
            if bubble.role == MessageRole.assistant:
                return [
                    b.text for b in bubble.blocks
                    if isinstance(b, ProseBlock) and b.kind == PartType.text
                ]
        return []


def _render_history_bubbles(messages: list[dict]) -> list[Bubble]:
    """Tool results have no block: they are dropped here and never rendered on reload."""
    return [
        Bubble(role=MessageRole(message["role"]), blocks=_blocks_in_turn_order(message))
        for message in messages
        if message.get("role") in (MessageRole.user, MessageRole.assistant)
    ]


def _blocks_in_turn_order(message: dict) -> list[ProseBlock | ToolBlock]:
    """Reading order is the order the turn produced, so text after a tool call renders after it."""
    blocks: list[ProseBlock | ToolBlock] = []
    for part in message.get("parts") or []:
        part_type = part.get("type")
        if part_type == PartType.tool_call:
            blocks.append(ToolBlock(name=part.get("name", ""), args=part.get("args", ""),
                                    label=part.get("label") or part.get("name", "")))
        elif part_type == PartType.text:
            _append_prose(blocks, "text", part.get("text", ""))
        elif part_type == PartType.thinking:
            _append_prose(blocks, "thinking", part.get("text", ""))
    return blocks


def _append_prose(blocks: list[ProseBlock | ToolBlock], kind: Literal["text", "thinking"],
                  text: str) -> None:
    """One block per RUN of a kind: the split the live stream renders, and the swap compares."""
    previous = blocks[-1] if blocks else None
    if isinstance(previous, ProseBlock) and previous.kind == kind:
        previous.text += text
    else:
        blocks.append(ProseBlock(kind=kind, text=text))
