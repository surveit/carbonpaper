"""What one STAGE of a run records, and the pure predicates over a raw manifest
payload. The manifest itself is a stored record — `app.runtime.manifest`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict

from app.core.agent.usage import LlmUsage
from app.core.run_status import StageStatus
from app.models.stage import Stage
from app.models.stages.stage_base import StageType


class QueueStats(TypedDict):
    """One human_review_queue stage's tallies, recorded on the manifest under
    `human_review_queue_stats[stage_id]`."""

    items_queued_total: int
    items_passed_through: int
    items_pending: int
    items_decided: int


class RowError(TypedDict):
    """One row's generation failure: its 0-based position and the message."""

    row: int
    message: str


class StageContribution(BaseModel):
    """What one stage's handler contributes back to the run manifest: token
    usage (folded onto the stage's `StageRecord.llm_usage`), per-row generation
    errors (returned for the executor to fold into the stage's validation report
    and terminal status), dropped-column notes and human-review-queue tallies
    (folded onto the manifest's per-stage `dropped_columns`/
    `human_review_queue_stats` maps), and free-text run notes (appended to the
    stage record's `notes`). Empty for a stage that contributes none.
    Not stage data — the manifest fields a handler owns."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_usage: LlmUsage | None = None
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
    """A stage's `error` field once it has failed: the exception's type name, a
    human-readable message, and its traceback — `None` for a row-generation
    error, which has no single exception to format."""

    type: str
    message: str
    traceback: str | None


class StageRecord(BaseModel):
    """One stage's manifest record, written into `manifest["stage_records"]` and
    read back by the web layer.

    Field order is the on-disk field order of a stage that RAN;
    `output_path`, `queue_path`, `notes`, and `llm_usage` are set only at the
    lifecycle points that produce them (`_finalize_stage_output` adds
    `output_path`/`llm_usage`, `_record_halt` adds `queue_path`, row slicing and
    the CSV fallback add `notes`) and are omitted from the JSON until then.
    `finished_at` is set to `None` up front for a pending record and to a
    timestamp once a running stage settles; it is omitted from a mid-run flush of
    a stage that has started but not yet settled.

    `input_validation_report`/`output_validation_report` each hold the dict form
    of a `ValidationReport` (app.runtime.validation) — errors AND warnings, not
    errors alone — whose own fields are untyped in that module. The input side is
    a list: one report per upstream input that declares a schema.

    The three fields the run's own bookkeeping always writes
    (`input_validation_report`, `output_validation_report`, `output_row_count`)
    carry no default: they were renamed out of an older on-disk vocabulary, and a
    default would let a pre-rename `manifest.json` parse and then report a
    fabricated zero/empty value. Required, such a file fails loudly at parse."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stage_id: str
    type: StageType
    started_at: str | None = None
    status: StageStatus
    input_validation_report: list[dict[str, object]]
    output_validation_report: dict[str, object] | None
    elapsed_ms: int = 0
    output_row_count: int
    error: StageErrorInfo | None = None
    llm_usage: LlmUsage | None = None
    notes: list[str] | None = None
    output_path: str | None = None
    queue_path: str | None = None
    finished_at: str | None = None

    @classmethod
    def record_with_status(cls, stage: Stage, status: StageStatus) -> StageRecord:
        """A fresh record for `stage` in `status`: no output, no validation, no
        elapsed time yet. A RUNNING record is stamped with `started_at` (the
        stage is starting now); any other status — PENDING, for a stage the run
        has not reached or has marked blocked — leaves `started_at` None, and
        also pins `finished_at` to None so the never-started record carries the
        key explicitly."""
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
        """Append a run note (a row-slicing trim, a CSV fallback), materializing
        the `notes` list on first use so a stage that produced none omits the
        field entirely."""
        if self.notes is None:
            self.notes = []
        self.notes.append(note)


def records_a_test_run(raw: dict[str, Any]) -> bool:
    """Both on-disk shapes, for a caller that must not pay to parse the whole model."""
    nested = raw.get("parameters")
    if isinstance(nested, dict) and "is_test_run" in nested:
        return bool(nested["is_test_run"])
    return bool(raw.get("is_test_run", False))


def read_run_bindings(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Both on-disk shapes, for a caller that must not pay to parse the whole model."""
    nested = raw.get("parameters")
    if isinstance(nested, dict) and "run_bindings" in nested:
        return dict(nested["run_bindings"] or {})
    return dict(raw.get("run_bindings") or {})
