"""What a run RECORDS: the manifest's typed shape and the pure predicates over it.

Minting one, writing it to disk, and reading a stage's output frame back are the
runtime's job (`app.runtime.manifest`) — a reader that only needs the shape, or
one raw-dict fact off it, does not have to reach into the runtime for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from app.core.agent.usage import LlmUsage
from app.core.run_status import RunStatus, StageStatus
from app.models.run_parameters import RunParameters
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


class RunManifest(BaseModel):
    """The run's living record. Minted by `create_run_manifest` (all stages
    pending, status running), mutated per stage by the executor, and flushed to
    disk by `write_manifest`. The optional run-level fields (`updated_at` on a
    mid-run flush, `finished_at`/`halted_at`/`cancelled_at` at finalization,
    `resumed_at` on a resume) are set only when they apply and omitted from the
    JSON otherwise.

    This is a run-directory file artifact, not a document-store model: it lives
    as `manifest.json` inside the run dir alongside that run's `outputs/` and
    `artifacts/`, and is read back by path (`load_manifest_model`). It is not a
    `PersistedModel` and has no row in the document store."""

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
        """Lifted, never defaulted: a real run's recorded settings are not ours to invent."""
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
        """Normalize a legacy scalar `halted_at` (a bare stage-id string a
        pre-fork-aware run persisted) into a one-element list, so every consumer
        sees one shape."""
        if isinstance(value, str):
            return [value]
        return value

    def settle_stage_records(self, records: list[StageRecord]) -> None:
        """Replace the manifest's per-stage records with `records` — the executor
        hands back the final set in topological order once the run loop stops,
        overwriting the pending records minted at the start."""
        self.stage_records = records

    def record_dropped_columns(self, stage_id: str, columns: list[str]) -> None:
        """Record `stage_id`'s dropped output columns under `dropped_columns`,
        keeping the field marked set so serialization emits it even on a resumed
        legacy manifest that reached this run without the key."""
        self.dropped_columns[stage_id] = columns
        self.__pydantic_fields_set__.add("dropped_columns")

    def record_human_review_queue_stats(self, stage_id: str, stats: QueueStats) -> None:
        """Record `stage_id`'s human-review-queue tallies under
        `human_review_queue_stats`."""
        self.human_review_queue_stats[stage_id] = stats

    def clear_halt(self) -> None:
        """Drop the halt marker: this run is no longer awaiting review. Clears
        `halted_at` from the model's set-fields (and resets it to None), so
        `exclude_unset` writes no such key — a resume that is re-running the
        halted stage, or a cancel that supersedes an earlier halt, must not leave
        the run page showing a review banner for a halt that no longer holds."""
        self.halted_at = None
        self.__pydantic_fields_set__.discard("halted_at")

    def find_stage_record(self, stage_id: str) -> StageRecord | None:
        """This run's record for `stage_id`, or None if the run has no such
        stage."""
        return next((r for r in self.stage_records if r.stage_id == stage_id), None)

    def to_dict(self) -> dict[str, Any]:
        """This manifest as a plain dict, with unset optional fields omitted — the
        boundary shape the web and service readers consume, and the shape
        `write_manifest` serializes. Uses Python mode, so a status stays the
        `RunStatus`/`StageStatus` enum member the in-memory run carried (each is a
        `str` enum, so `json.dumps` still writes it as its bare string value on
        disk); nested models (a stage's `llm_usage`) become plain dicts."""
        return self.model_dump(exclude_unset=True)


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
