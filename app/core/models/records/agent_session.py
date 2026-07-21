"""AgentSession: one chat session — metadata plus one engine-agnostic
transcript (a list of ``{role, parts}`` messages, part types
``text|thinking|tool_call|tool_result``) plus the resume token that carries
the agent's cross-turn memory.

Defined here (not in app.core.agent.store, which owns the SessionStore
adapter over it) so it sits alongside the other records; see
app.core.models.records for why a record — unlike the pure contracts
alongside app.core.models — may import PersistedModel."""
from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from app.core.persistence import PersistedModel


class AgentSession(PersistedModel):
    """A chat session: metadata, the bound agent + its context, and the stored
    transcript. `id` (inherited from PersistedModel) is the session id."""

    collection: ClassVar[str] = "agent_session"
    title: str = "New chat"
    agent_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    messages: list[dict[str, Any]] = Field(default_factory=list)  # engine-neutral {role, parts} transcript
    active_turn: str | None = None
    pending_user: str | None = None
    sdk_session_id: str | None = None  # resume token (CLI session to resume)
