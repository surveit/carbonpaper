"""What a capture records beside its archive: the run's own settings and decisions."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.ids import ID
from app.core.json_types import JsonDict
from app.models.run_parameters import RunParameters
from app.models.stages.human_review_queue import ReviewVerdict


class CapturedDecision(BaseModel):
    """A ReviewDecision minus its project: a restore re-records it under its own."""

    stage_id: ID
    stage_fingerprint: str
    input_fingerprint: str
    frozen_input: JsonDict
    verdict: ReviewVerdict
    reviewed_values: JsonDict
    review_notes: str | None
    reviewer: str
    reviewed_at: str


class CapturedRun(BaseModel):
    # Only the three a restore replays; the rest name this workspace's own project.
    parameters: RunParameters
    decisions: list[CapturedDecision] = []
