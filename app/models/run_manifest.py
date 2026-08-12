"""What a run RECORDS: the manifest's typed shape, the pure predicates over it, and
the one read of a run's manifest.json off disk.

Minting one, writing it back, and reading a stage's output frame are the runtime's
job (`app.runtime.manifest`).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.agent.usage import LlmUsage
from app.core.errors import RunManifestNotJson
from app.core.run_status import RunStatus, StageStatus
from app.models.run_parameters import RunParameters
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


# RunParameters field -> the top-level key a manifest written before the nesting
# carried it under. Read by `_lift_legacy_parameters`; may only grow.
_LEGACY_PARAMETER_KEYS = {
    "limits": "limit_overrides",
    "offsets": "offset_overrides",
    "bust_cache": "bust_cache",
    "queue_auto_approve": "queue_auto_approve",
    "is_test_run": "is_test_run",
    "run_bindings": "run_bindings",
}


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


class RunManifest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    started_at: str
    project: str | None
    workflow_version: str | None
    # What the caller asked of this run, verbatim — the settings a resume replays.
    # `_lift_legacy_parameters` reads the flat pre-nesting keys off an older
    # manifest into it, so every run on disk still parses.
    parameters: RunParameters = RunParameters()
    # The preflight provenance of each bound input (its absolute path, a sha256 and
    # a byte count streamed at prepare time). A RESULT, not a parameter: it records
    # what the run found, not what it was asked for.
    input_bindings: dict[str, dict[str, Any]] = {}
    # The live human_review_queue tallies. Required, unlike the override maps
    # above: the key was renamed out of an older on-disk vocabulary, so a default
    # would let a pre-rename manifest parse and then report an empty tally for a
    # run that actually queued items. `create_run_manifest` always sets it.
    human_review_queue_stats: dict[str, QueueStats]
    dropped_columns: dict[str, list[str]] = {}
    status: RunStatus
    stage_records: list[StageRecord]
    updated_at: str | None = None
    finished_at: str | None = None
    halted_at: list[str] | None = None
    cancelled_at: str | None = None
    resumed_at: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _lift_legacy_parameters(cls, data: Any) -> Any:
        if not isinstance(data, dict) or "parameters" in data:
            return data
        legacy = {
            new: data[old]
            for new, old in _LEGACY_PARAMETER_KEYS.items()
            if old in data
        }
        return {**data, "parameters": legacy} if legacy else data

    @field_validator("halted_at", mode="before")
    @classmethod
    def _halted_at_to_list(cls, value: object) -> object:
        if isinstance(value, str):
            return [value]
        return value

    def settle_stage_records(self, records: list[StageRecord]) -> None:
        self.stage_records = records

    def record_dropped_columns(self, stage_id: str, columns: list[str]) -> None:
        self.dropped_columns[stage_id] = columns
        # Marked set so `exclude_unset` still emits it on a legacy manifest that lacked the key.
        self.__pydantic_fields_set__.add("dropped_columns")

    def record_human_review_queue_stats(self, stage_id: str, stats: QueueStats) -> None:
        self.human_review_queue_stats[stage_id] = stats

    def clear_halt(self) -> None:
        self.halted_at = None
        # Unmarked so `exclude_unset` writes no key: a resumed or cancelled run must not
        # show a review banner for a halt that no longer holds.
        self.__pydantic_fields_set__.discard("halted_at")

    def find_stage_record(self, stage_id: str) -> StageRecord | None:
        return next((r for r in self.stage_records if r.stage_id == stage_id), None)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude_unset=True)


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

_MANIFEST_FILENAME = "manifest.json"


def find_manifest_backed_run_dirs(runs_dir: Path) -> list[Path]:
    if not runs_dir.is_dir():
        return []
    return sorted(
        (run for run in runs_dir.iterdir()
         if run.is_dir() and (run / _MANIFEST_FILENAME).exists()),
        key=lambda run: run.name,
    )


def read_run_manifest_json(run_dir: Path) -> dict[str, Any]:
    path = run_dir / _MANIFEST_FILENAME
    if not path.exists():
        raise FileNotFoundError(f"No manifest at {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RunManifestNotJson(f"{path} does not parse as JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise RunManifestNotJson(f"{path} holds a JSON {type(raw).__name__}, not an object")
    return raw


def read_run_manifest(run_dir: Path) -> RunManifest:
    return RunManifest.model_validate(read_run_manifest_json(run_dir))
