"""What one STAGE of a run records, and the pure predicates over a raw manifest
payload. The manifest itself is a stored record — `app.runtime.manifest`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePath
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.agent.usage import LlmUsage
from app.core.run_status import StageStatus
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stage import Stage
from app.models.stages.stage_base import StageType
from app.core.ids import ID


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


class StageProgress(BaseModel):
    model_config = ConfigDict(frozen=True)

    completed: int = Field(ge=0, strict=True)
    total: int | None = Field(default=None, ge=0, strict=True)
    updated_at: str

    @model_validator(mode="after")
    def _completed_does_not_exceed_total(self) -> StageProgress:
        if self.total is not None and self.completed > self.total:
            raise ValueError(
                f"completed progress {self.completed} exceeds total {self.total}"
            )
        return self


# The stage statuses whose output frame holds what the stage promised. An `error`
# stage also wrote a frame, but its untouched columns are nulls rather than results.
FINISHED_STAGE_STATUSES = (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)

# What a READER states for a manifest that will not parse. Not a RunStatus member:
# no run records it, and a stored status this model rejected is how it is reached.
UNREADABLE_RUN_STATUS = "corrupt"


class StageRecord(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    stage_id: ID
    type: StageType
    started_at: str | None = None
    status: StageStatus
    # No default here or on `output_row_count`: these were renamed out of an older on-disk
    # vocabulary, and a default would let a pre-rename manifest parse and then report a
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
    progress: StageProgress | None = None

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


class InputBinding(BaseModel):
    """One file a run read, as its preflight recorded it."""

    stage_id: ID
    path: str
    filename: str
    # None where preflight never measured it, not zero.
    sha256: str | None = None
    bytes: int | None = None
    source: str | None = None


def read_input_bindings(raw: dict[str, Any]) -> list[InputBinding]:
    bindings = raw.get("input_bindings") or {}
    return [
        _read_one_binding(str(stage_id), binding)
        for stage_id, binding in sorted(bindings.items())
        if isinstance(binding, dict)
    ]


def _read_one_binding(stage_id: ID, binding: dict[str, Any]) -> InputBinding:
    path = str(binding.get("path") or "")
    size = binding.get("bytes")
    return InputBinding(
        stage_id=stage_id,
        path=path,
        filename=PurePath(path).name,
        sha256=_read_optional_text(binding.get("sha256")),
        bytes=size if isinstance(size, int) else None,
        source=_read_optional_text(binding.get("source")),
    )


def _read_optional_text(value: Any) -> str | None:
    return str(value) if value else None
