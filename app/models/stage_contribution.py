"""What a stage HANDS BACK as it runs, before anything records it. Separate from
`app.models.run_manifest` so the runtime's leaf modules (errors, stage_output)
can name this without reaching the stored-manifest models.
"""

from __future__ import annotations

from typing import TypedDict

from pydantic import BaseModel, ConfigDict

from app.core.agent.usage import LlmUsage


class QueueStats(TypedDict):
    items_queued_total: int
    items_passed_through: int
    items_pending: int
    items_decided: int


class RowError(TypedDict):
    """`row` is a 0-based position in the frame."""

    row: int
    message: str


class StageContribution(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_usage: LlmUsage | None = None
    # How many of the stage's output rows the row cache answered instead of the
    # stage computing them. None where the stage ran uncached, so a stage that
    # could not have replayed anything is never reported as having replayed zero.
    cached_rows: int | None = None
    row_errors: list[RowError] = []
    dropped_columns: list[str] = []
    human_review_queue_stats: QueueStats | None = None
    # Non-fatal facts about how the stage ran, appended to the stage record's
    # own `notes` (where the executor also writes its row-slicing and CSV-
    # fallback notes). Never stage data.
    notes: list[str] = []
