"""The run manifest: the one legitimately-mutable run object, and its on-disk shape.

The executor (`app.runtime.executor`) is its single writer; every other layer
reads it back. Serialization is `exclude_unset`, so an optional field appears
on disk only once the run reaches the point that sets it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

import pandas as pd
from pydantic import BaseModel, ConfigDict, field_validator

from app.core.agent.usage import LlmUsage
from app.core.errors import StageNotInRun, StageOutputMissing
from app.core.frames import read_frame_file
from app.models import Stage, StageType
from app.core.run_status import RunStatus, StageStatus

from .context import RunContext


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


# The `.attrs` key a stage's output frame carries its StageContribution under.
CONTRIBUTION_ATTR = "stage_contribution"


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
    name: str
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
            stage_id=stage.id, type=stage.type, name=stage.name,
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
    # The per-run override maps. Defaulted so a partial or legacy manifest that
    # predates one of them still parses on resume (the old dict readers tolerated
    # the same via `.get(key, {})`); a freshly-minted manifest always sets every
    # one, so `exclude_unset` still emits them.
    limit_overrides: dict[str, int] = {}
    offset_overrides: dict[str, int] = {}
    run_bindings: dict[str, dict[str, Any]] = {}
    input_bindings: dict[str, dict[str, Any]] = {}
    # Whether this run skipped every stage-cache read (RunContext.bust_cache).
    # Recorded so it is part of the run's provenance and so a resume replays the
    # same choice; defaulted for the same legacy-manifest reason as the maps above.
    bust_cache: bool = False
    # True for a workflow test: a run that lives under the same runs/ dir and is
    # viewable through the same routes as a production run, but is a test — it
    # wrote no stage-cache entries and its numbers must never be mistaken for the
    # project's latest real run (see app.services.project.RunsSummary). Defaults
    # False so a pre-this-field manifest on disk (every run predates the field)
    # parses as what it always was: not a test.
    is_test_run: bool = False
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


def create_run_manifest(
    ordered: list[Stage],
    ctx: RunContext,
    *,
    run_id: str,
    project: str | None,
    workflow_version: str | None,
    run_bindings: dict[str, dict[str, Any]],
    input_bindings: dict[str, dict[str, Any]],
    is_test_run: bool,
) -> RunManifest:
    """The initial run manifest — every stage pending, status running. The single
    source of the run-manifest shape: every caller mints it here and persists it
    with write_manifest rather than hand-building the model, so the shape lives
    with the engine that later updates it.

    The per-run execution settings the manifest RECORDS — the row windows and
    `bust_cache` — are read off `ctx`, the same object the engine executes against,
    so a caller cannot set one and record another.

    `project`/`workflow_version` are None for a subset run (run_subset) that was
    not told its logical identity — recorded honestly as None rather than a
    fabricated placeholder. A production run always supplies both. `is_test_run`
    is required of every caller (no default) so minting a manifest forces a
    conscious choice, unlike the field's own legacy-tolerant default on
    `RunManifest`. `human_review_queue_stats` and `dropped_columns` start empty
    and grow live as stages settle (the executor drains each stage's
    StageContribution into them)."""
    return RunManifest(
        run_id=run_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project,
        workflow_version=workflow_version,
        limit_overrides=ctx.limits,
        offset_overrides=ctx.offsets,
        run_bindings=run_bindings,
        input_bindings=input_bindings,
        bust_cache=ctx.bust_cache,
        is_test_run=is_test_run,
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stage_records=[
            StageRecord.record_with_status(s, StageStatus.PENDING) for s in ordered
        ],
    )


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    """The single writer of run_dir/manifest.json. The initial write (prepare_run),
    every mid-run flush, and finalization all persist through here — dumping the
    typed model to the same `exclude_unset` JSON shape a reader parses back."""
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, default=str), encoding="utf-8"
    )


def resolve_output_path(run_dir: Path, output_path: str | None) -> Path | None:
    """The sole join of a run dir to a recorded output path; None when the record names none."""
    if not output_path:
        return None
    resolved = (run_dir / output_path).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise StageOutputMissing(
            f"recorded output path '{output_path}' escapes run '{run_dir.name}'"
        )
    return resolved


def read_stage_output_frame(run_dir: Path, stage_id: str) -> pd.DataFrame:
    """The frame a stage of this run wrote, read from the path its own record names."""
    records = load_manifest_model(run_dir).stage_records
    record = _find_stage_record(records, run_dir, stage_id)
    path = resolve_output_path(run_dir, record.output_path)
    if path is None:
        raise StageOutputMissing(
            f"stage '{stage_id}' of run '{run_dir.name}' wrote no output "
            f"(its status is '{record.status}'), so it holds no values to read"
        )
    return read_frame_file(path)


def _find_stage_record(
    records: list[StageRecord], run_dir: Path, stage_id: str
) -> StageRecord:
    for record in records:
        if record.stage_id == stage_id:
            return record
    ran = ", ".join(record.stage_id for record in records) or "(none)"
    raise StageNotInRun(
        f"run '{run_dir.name}' has no stage '{stage_id}' — the stages it ran: {ran}"
    )


def load_manifest_model(run_dir: Path) -> RunManifest:
    """Parse a run's `manifest.json` off disk into a `RunManifest`, applying the
    model's normalization (a legacy scalar `halted_at` becomes a one-element
    list). Raises FileNotFoundError if the run has no manifest."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest at {manifest_path}")
    return RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
