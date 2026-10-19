"""The judgment ledger: what a model was asked about one row, which model answered, and what."""
from __future__ import annotations

import enum
from typing import ClassVar

from pydantic import BaseModel, ConfigDict

from app.core.agent.usage import LlmUsage
from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope


class JudgmentKind(enum.StrEnum):
    MODEL = "model"


class JudgmentDraft(BaseModel):
    """One answered model call, before the row driver records it against the row it decided."""

    model_config = ConfigDict(frozen=True)

    system_prompt: str
    task: str
    model: str
    reply: JsonDict
    # Every attempt this call made, the failed ones included: those tokens were spent.
    usage: LlmUsage
    decided_at: str


class Judgment(PersistedModel):
    """Written only through `StageCache.record_judgment`; a cache entry names it by `judgment_id`."""

    collection: ClassVar[str] = "judgment"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ_WRITE

    project_id: ID
    run_id: ID
    stage_id: ID
    kind: JudgmentKind
    input_fingerprint: str
    frozen_input: JsonDict
    system_prompt: str
    task: str
    model: str
    reply: JsonDict
    usage: LlmUsage
    decided_at: str

    @classmethod
    def read_only(cls) -> ReadOnlyJudgments:
        return ReadOnlyJudgments()


class ReadOnlyJudgments:
    def get(self, judgment_id: ID) -> Judgment | None:
        return Judgment.load_or_none(judgment_id)
