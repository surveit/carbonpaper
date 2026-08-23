"""What a stage hands back as it runs, before anything records it."""

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
    # Output rows the row cache answered. None where the stage ran uncached, never zero.
    cached_rows: int | None = None
    row_errors: list[RowError] = []
    dropped_columns: list[str] = []
    human_review_queue_stats: QueueStats | None = None
    # Non-fatal facts about how the stage ran, appended to the record's `notes`.
    # Never stage data.
    notes: list[str] = []
