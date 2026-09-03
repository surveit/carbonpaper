from __future__ import annotations

from typing import ClassVar

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.core.record import PersistedModel, PersistenceScope
from app.models.stages.human_review_queue import ReviewVerdict


class ReviewDecision(PersistedModel):
    """Append-only: a correction is a new row; nothing here is ever edited or deleted."""

    collection: ClassVar[str] = "review_decision"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project: str
    stage_id: ID
    stage_fingerprint: str
    input_fingerprint: str
    frozen_input: JsonDict
    verdict: ReviewVerdict
    reviewed_values: JsonDict
    review_notes: str | None
    reviewer: str
    reviewed_at: str
    # Absent for a caller with no run context; present whenever a run pinned one.
    workflow_version: str | None = None
