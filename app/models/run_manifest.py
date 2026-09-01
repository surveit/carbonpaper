"""What one STAGE of a run records, and the pure predicates over a raw manifest
payload. The manifest itself is a stored record — `app.runtime.manifest`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePath
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.agent.usage import LlmUsage
from app.core.run_status import StageStatus
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stage import Stage
from app.models.stages.stage_base import StageType
from app.core.ids import ID


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


class ReadFile(BaseModel):
    """One file a stage read, as its preflight weighed it."""

    path: str
    sha256: str
    bytes: int
    # None outside the store, and on every run recorded before this field existed.
    file_id: str | None = None

    @property
    def filename(self) -> str:
        return PurePath(self.path).name


class StageInputRecord(BaseModel):
    """What one input stage read this run — the shape a manifest carries under its id."""

    files: list[ReadFile]
    source: str | None = None


class InputBinding(BaseModel):
    """One file a run read, flattened out of its stage's record."""

    stage_id: ID
    path: str
    filename: str
    # None where an older manifest recorded no measurement, not zero.
    sha256: str | None = None
    bytes: int | None = None
    source: str | None = None
    file_id: str | None = None


def read_input_bindings(raw: dict[str, Any]) -> list[InputBinding]:
    """One entry per FILE, so a stage that read several contributes several."""
    recorded = raw.get("input_bindings") or {}
    return [
        binding
        for stage_id, record in sorted(recorded.items())
        if isinstance(record, dict)
        for binding in _read_one_stages_files(str(stage_id), record)
    ]


def _read_one_stages_files(stage_id: ID, record: dict[str, Any]) -> list[InputBinding]:
    files, source = _files_and_source(record)
    return [
        # `source` sits on the stage's record; every file it read was bound the same way.
        InputBinding(stage_id=stage_id, path=f.path, filename=f.filename,
                     sha256=f.sha256, bytes=f.bytes, source=source, file_id=f.file_id)
        for f in files
    ]


def _files_and_source(record: dict[str, Any]) -> tuple[list[ReadFile], str | None]:
    """`files` is the shape written today; a manifest without it is a run from before it."""
    source = _read_optional_text(record.get("source"))
    if "files" in record:
        return StageInputRecord.model_validate(record).files, source
    return _read_pre_files_record(record), source


def _read_pre_files_record(record: dict[str, Any]) -> list[ReadFile]:
    """A run recorded before an input could read several files: the record IS the one file."""
    path = str(record.get("path") or "")
    if not path:
        return []
    size = record.get("bytes")
    return [ReadFile(path=path, sha256=str(record.get("sha256") or ""),
                     bytes=size if isinstance(size, int) else 0)]


def _read_optional_text(value: Any) -> str | None:
    return str(value) if value else None
