"""What one STAGE of a run records, and the pure predicates over a raw manifest
payload. The manifest itself is a stored record — `app.runtime.manifest`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict

from app.core.agent.usage import LlmUsage
from app.core.run_status import StageStatus
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stage import Stage
from app.models.stages.stage_base import StageType


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


# The `StageErrorInfo.type` a stage carries when its own OUTPUT does not satisfy the
# schema it declares. Nothing was raised: the data is not what the stage says it is,
# which is the data owner's to fix, not the author of the code's.
SCHEMA_REFUSAL_ERROR_TYPE = "OutputSchemaViolation"


class StageErrorInfo(BaseModel):
    type: str
    message: str
    traceback: str | None


class StageRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    stage_id: str
    type: StageType
    started_at: str | None = None
    status: StageStatus
    # No default here or on `output_row_count`: these were renamed out of an older on-disk
    # vocabulary, and a default would let a pre-rename manifest.json parse and then report a
    # fabricated zero/empty value. Required, such a file fails loudly at parse.
    # One `ValidationReport` dict (app.runtime.validation) per side; the input side is one
    # per upstream input that declares a schema.
    input_validation_report: list[dict[str, object]]
    output_validation_report: dict[str, object] | None
    elapsed_ms: int = 0
    output_row_count: int
    error: StageErrorInfo | None = None
    llm_usage: LlmUsage | None = None
    # Optional, and absent from every manifest written before it existed: a
    # record with no `cached_rows` means the count was never taken, not zero.
    cached_rows: int | None = None
    notes: list[str] | None = None
    output_path: str | None = None
    queue_path: str | None = None
    finished_at: str | None = None

    @classmethod
    def record_with_status(cls, stage: Stage, status: StageStatus) -> StageRecord:
        running = status is StageStatus.RUNNING
        record = cls(
            stage_id=stage.id, type=stage.type,
            started_at=datetime.now().isoformat(timespec="seconds") if running else None,
            status=status,
            input_validation_report=[], output_validation_report=None,
            elapsed_ms=0, output_row_count=0, error=None,
        )
        if not running:
            record.finished_at = None
        return record

    def add_note(self, note: str) -> None:
        if self.notes is None:
            self.notes = []
        self.notes.append(note)


def records_a_test_run(raw: dict[str, Any]) -> bool:
    nested = raw.get("parameters")
    if isinstance(nested, dict) and "is_test_run" in nested:
        return bool(nested["is_test_run"])
    return bool(raw.get("is_test_run", False))


def read_run_bindings(
    raw: dict[str, Any]
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    nested = raw.get("parameters")
    if isinstance(nested, dict) and "run_bindings" in nested:
        return dict(nested["run_bindings"] or {})
    return dict(raw.get("run_bindings") or {})


# ─── Reading a run's manifest off disk ────────────────────────────────────────
# The only place a run directory's manifest.json is located, read and JSON-parsed.
# `read_run_manifest_json` hands back the raw object, so a caller needing one fact
# off a manifest this model would reject (a partial or pre-rename file already on
# disk) still gets it; `read_run_manifest` adds the typed validation on top.
# Neither decides what an unreadable manifest MEANS — each caller answers that.

