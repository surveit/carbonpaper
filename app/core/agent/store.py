"""Chat session store: an `AgentSession` record holding one engine-agnostic
transcript (``{role, parts}`` messages) plus the agent's resume token.

In-flight turns live in memory (app.core.agent.turns); surviving a server
restart mid-turn is out of scope.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar, Literal, TypedDict
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from app.core.agent.usage import LlmUsage, TurnSpend
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.ids import ID


class MessageRole(str, Enum):
    user = "user"
    assistant = "assistant"


class PartType(str, Enum):
    text = "text"
    thinking = "thinking"
    tool_call = "tool_call"
    tool_result = "tool_result"
    offer = "offer"


class TranscriptMessage(TypedDict):
    """One stored transcript message; `role` is a MessageRole value."""

    role: str
    parts: list[dict[str, Any]]


class ProseBlock(BaseModel):
    kind: Literal["text", "thinking"]
    text: str


class ToolCall(BaseModel):
    name: str
    args: str
    label: str


class ToolBlock(BaseModel):
    kind: Literal["tool"] = "tool"
    calls: list[ToolCall]


# Drawn as buttons, not a tool row: each option is a message the reader may send.
OFFER_NEXT_STEPS = "offer_next_steps"


class Offer(BaseModel):
    """A reply the reader may click. Carrying a url it opens that page instead."""

    text: str = Field(min_length=1, max_length=70)
    url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _read_a_bare_reply(cls, data: Any) -> Any:
        # A written offer is a plain string: it replies, so it carries no url.
        return {"text": data} if isinstance(data, str) else data

    @field_validator("url")
    @classmethod
    def _keep_only_the_path(cls, url: str | None) -> str | None:
        # Whatever host was written, the button goes to a page in this app.
        if url is None:
            return None
        parts = urlsplit(url)
        path = urlunsplit(("", "", parts.path, parts.query, parts.fragment))
        if not path.startswith("/"):
            raise ValueError(f"a link offer takes a path in this app, not {url!r}")
        return path


class NextSteps(BaseModel):
    """What one turn offers as the reader's next message, in the reader's own voice."""

    options: list[Offer] = Field(min_length=2, max_length=4)


class OffersBlock(BaseModel):
    kind: Literal["offers"] = "offers"
    options: list[Offer]


class Bubble(BaseModel):
    role: MessageRole
    blocks: list[ProseBlock | ToolBlock | OffersBlock]


class AgentSession(PersistedModel):
    collection: ClassVar[str] = "agent_session"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    title: str = "New chat"
    agent_id: ID | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)  # engine-neutral {role, parts} transcript
    active_turn: str | None = None
    pending_user: str | None = None
    sdk_session_id: ID | None = None  # resume token (CLI session to resume)

    # One entry per turn that reported usage, appended as the turn tears down. A
    # session written before this field carries none: what those turns cost was
    # never recorded, which is not the same as their having cost nothing.
    turn_spend: list[TurnSpend] = Field(default_factory=list)


def open_session_store() -> SessionStore:
    return SessionStore()


class SessionStore:
    def create(
        self,
        *,
        title: str | None = None,
        agent_id: ID | None = None,
        context: dict | None = None,
    ) -> str:
        session_id = uuid.uuid4().hex[:12]
        AgentSession(
            id=session_id,
            title=title or "New chat",
            agent_id=agent_id,
            context=context or {},
        ).save()
        return session_id

    def exists(self, session_id: ID) -> bool:
        return AgentSession.exists(session_id)

    def load(self, session_id: ID) -> dict:
        session = AgentSession.load(session_id)
        return {"session_id": session.id, **session.model_dump(exclude={"id"})}

    def load_messages(self, session_id: ID) -> list[dict[str, Any]]:
        """Always empty: cross-turn memory comes from the CLI resume token, not from a replayed transcript."""
        del session_id
        return []

    def append_messages(self, session_id: ID, messages: list[dict[str, Any]]) -> None:
        """A turn contributes its own messages; the earlier turns stay on the page."""
        session = AgentSession.load(session_id)
        session.messages = [*session.messages, *messages]
        session.pending_user = None
        session.save()

    def set_active_turn(self, session_id: ID, turn_id: ID | None) -> None:
        session = AgentSession.load(session_id)
        session.active_turn = turn_id
        session.save()

    def resume_token(self, session_id: ID) -> str | None:
        return AgentSession.load(session_id).sdk_session_id

    def set_resume_token(self, session_id: ID, token: str) -> None:
        session = AgentSession.load(session_id)
        session.sdk_session_id = token
        session.save()

    def record_turn_spend(self, session_id: ID, usage: LlmUsage) -> None:
        session = AgentSession.load(session_id)
        stamp = datetime.now().isoformat(timespec="seconds")
        session.turn_spend = [*session.turn_spend, TurnSpend(created_at=stamp, usage=usage)]
        session.save()

    def set_pending_user(self, session_id: ID, text: str | None) -> None:
        session = AgentSession.load(session_id)
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

    def history_view(self, session_id: ID) -> list[Bubble]:
        return _render_history_bubbles(AgentSession.load(session_id).messages)

    def read_last_reply_texts(self, session_id: ID) -> list[str]:
        """The newest reply's text blocks in turn order; empty when it only called tools."""
        for bubble in reversed(self.history_view(session_id)):
            if bubble.role == MessageRole.assistant:
                return [
                    b.text for b in bubble.blocks
                    if isinstance(b, ProseBlock) and b.kind == PartType.text
                ]
        return []


def read_opening_message(messages: list[dict[str, Any]]) -> str:
    """The agent's written first turn; "" when the transcript opens with the reader instead."""
    first = messages[0] if messages else {}
    if first.get("role") != MessageRole.assistant:
        return ""
    return "\n\n".join(
        part.get("text", "") for part in first.get("parts") or []
        if part.get("type") == PartType.text
    )


def _render_history_bubbles(messages: list[dict]) -> list[Bubble]:
    """Tool results have no block: they are dropped here and never rendered on reload."""
    return [
        Bubble(role=MessageRole(message["role"]), blocks=_blocks_in_turn_order(message))
        for message in messages
        if message.get("role") in (MessageRole.user, MessageRole.assistant)
    ]


def _blocks_in_turn_order(message: dict) -> list[ProseBlock | ToolBlock | OffersBlock]:
    """Reading order is the order the turn produced, so text after a tool call renders after it."""
    blocks: list[ProseBlock | ToolBlock | OffersBlock] = []
    for part in message.get("parts") or []:
        part_type = part.get("type")
        if part_type == PartType.offer:
            blocks.append(OffersBlock(options=part.get("options") or []))
        elif part_type == PartType.tool_call:
            _append_tool_call(blocks, ToolCall(
                name=part.get("name", ""), args=part.get("args", ""),
                label=part.get("label") or part.get("name", "")))
        elif part_type == PartType.text:
            _append_prose(blocks, "text", part.get("text", ""))
        elif part_type == PartType.thinking:
            _append_prose(blocks, "thinking", part.get("text", ""))
    return _drop_superseded_offers(blocks)


def _drop_superseded_offers(
    blocks: list[ProseBlock | ToolBlock | OffersBlock],
) -> list[ProseBlock | ToolBlock | OffersBlock]:
    """A turn offers one set of next steps; a second call replaces the first."""
    newest = next((b for b in reversed(blocks) if isinstance(b, OffersBlock)), None)
    return [b for b in blocks if not isinstance(b, OffersBlock) or b is newest]


def _append_tool_call(blocks: list[ProseBlock | ToolBlock | OffersBlock], call: ToolCall) -> None:
    """One block per RUN of a kind, as for prose: calls with no prose between them share a block."""
    previous = blocks[-1] if blocks else None
    if isinstance(previous, ToolBlock):
        previous.calls.append(call)
    else:
        blocks.append(ToolBlock(calls=[call]))


def _append_prose(blocks: list[ProseBlock | ToolBlock | OffersBlock],
                  kind: Literal["text", "thinking"],
                  text: str) -> None:
    """One block per RUN of a kind: the split the live stream renders, and the swap compares."""
    previous = blocks[-1] if blocks else None
    if isinstance(previous, ProseBlock) and previous.kind == kind:
        previous.text += text
    else:
        blocks.append(ProseBlock(kind=kind, text=text))
