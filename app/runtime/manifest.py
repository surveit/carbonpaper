"""The run manifest: the one legitimately-mutable run object, and its on-disk shape.

A run's `manifest.json` is the run's living record of what happened — minted when
the run starts (every stage pending), mutated per stage as the run proceeds, and
flushed to disk live so the run page shows progress. The executor
(`app.runtime.executor`) is its single writer; every other layer reads it back.

This module owns that shape as typed Pydantic models rather than a raw dict:

- `RunManifest` — the top-level run record (identity, per-run overrides, the
  live `queue_stats`/`dropped_columns` tallies, and the per-stage `stages`).
- `StageRecord` — one stage's record within `manifest["stages"]`, built pending
  (`StageRecord.pending`) or at start (`StageRecord.started`) and mutated as the
  stage settles.
- `StageContribution` — what a stage HANDLER contributes back to the manifest:
  its token usage (onto its `StageRecord.llm_usage`), its dropped-column and
  queue tallies (onto the manifest's per-stage maps), and its per-row generation
  errors (transient — folded into the stage's validation report and status, never
  stored as a field). A handler attaches one to its output frame's `.attrs` (or,
  when the queue stage halts before returning a frame, to the `HaltForReview` it
  raises); the executor MERGES it into the record/manifest it owns.

Serialization is `exclude_unset`: a field that was never set is omitted, so the
optional per-stage fields (`output_path`, `queue_path`, `notes`, `llm_usage`) and
the optional run-level fields (`finished_at`, `halted_at`, `cancelled_at`,
`resumed_at`, `updated_at`) appear on disk only once the run reaches the point
that sets them — reproducing the historical dict's key-presence exactly.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.agent.usage import LlmUsage
from app.models import Stage, StageType
from app.core.run_status import RunStatus, StageStatus


class QueueStats(TypedDict):
    """One human_review_queue stage's tallies, recorded on the manifest under
    `queue_stats[stage_id]`."""

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
    and terminal status), dropped-column notes and queue tallies (folded onto the
    manifest's per-stage `dropped_columns`/`queue_stats` maps). Empty for a stage
    that contributes none. Not stage data — the manifest fields a handler owns."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm_usage: LlmUsage | None = None
    row_errors: list[RowError] = []
    dropped_columns: list[str] = []
    queue_stats: QueueStats | None = None


class StageErrorInfo(BaseModel):
    """A stage's `error` field once it has failed: the exception's type name, a
    human-readable message, and its traceback — `None` for a row-generation
    error, which has no single exception to format."""

    type: str
    message: str
    traceback: str | None


class StageRecord(BaseModel):
    """One stage's manifest record, written into `manifest["stages"]` and read
    back by the web layer.

    Field order is the canonical on-disk order of a stage that RAN;
    `output_path`, `queue_path`, `notes`, and `llm_usage` are set only at the
    lifecycle points that produce them (`_finalize_stage_output` adds
    `output_path`/`llm_usage`, `_record_halt` adds `queue_path`, row slicing and
    the CSV fallback add `notes`) and are omitted from the JSON until then.
    `finished_at` is set to `None` up front for a pending record and to a
    timestamp once a running stage settles; it is omitted from a mid-run flush of
    a stage that has started but not yet settled. `input_validation`/
    `output_validation` hold `ValidationReport.to_dict()` output
    (app.runtime.validation), whose own fields are untyped in that module."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    stage_id: str
    type: StageType
    name: str
    started_at: str | None = None
    status: StageStatus
    input_validation: list[dict[str, object]] = []
    output_validation: dict[str, object] | None = None
    elapsed_ms: int = 0
    rows: int = 0
    error: StageErrorInfo | None = None
    llm_usage: LlmUsage | None = None
    notes: list[str] | None = None
    output_path: str | None = None
    queue_path: str | None = None
    finished_at: str | None = None

    @classmethod
    def pending(cls, stage: Stage) -> StageRecord:
        """A stage's record before it has started, or once it has been marked
        blocked: `pending` status, no output, no timing."""
        return cls(
            stage_id=stage.id, type=stage.type, name=stage.name,
            status=StageStatus.PENDING, input_validation=[], output_validation=None,
            elapsed_ms=0, rows=0, error=None, started_at=None, finished_at=None,
        )

    @classmethod
    def started(cls, stage: Stage) -> StageRecord:
        """A stage's record at the moment it starts running."""
        return cls(
            stage_id=stage.id, type=stage.type, name=stage.name,
            started_at=datetime.now().isoformat(timespec="seconds"),
            status=StageStatus.RUNNING, input_validation=[], output_validation=None,
            elapsed_ms=0, rows=0, error=None,
        )

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
    JSON otherwise."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    started_at: str
    project: str | None
    workflow_version: str | None
    # The per-run override maps and live tallies. Defaulted so a partial or
    # legacy manifest that predates one of them still parses on resume (the old
    # dict readers tolerated the same via `.get(key, {})`); a freshly-minted
    # manifest always sets every one, so `exclude_unset` still emits them.
    limit_overrides: dict[str, int] = {}
    offset_overrides: dict[str, int] = {}
    run_bindings: dict[str, dict[str, Any]] = {}
    input_bindings: dict[str, dict[str, Any]] = {}
    queue_stats: dict[str, QueueStats] = {}
    dropped_columns: dict[str, list[str]] = {}
    status: RunStatus
    stages: list[StageRecord]
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

    def settle_stages(self, records: list[StageRecord]) -> None:
        """Replace the manifest's per-stage records with `records` — the executor
        hands back the final set in topological order once the run loop stops,
        overwriting the pending records minted at the start."""
        self.stages = records

    def record_dropped_columns(self, stage_id: str, columns: list[str]) -> None:
        """Record `stage_id`'s dropped output columns under `dropped_columns`,
        keeping the field marked set so serialization emits it even on a resumed
        legacy manifest that reached this run without the key."""
        self.dropped_columns[stage_id] = columns
        self.__pydantic_fields_set__.add("dropped_columns")

    def record_queue_stats(self, stage_id: str, stats: QueueStats) -> None:
        """Record `stage_id`'s review-queue tallies under `queue_stats`, keeping
        the field marked set (see record_dropped_columns)."""
        self.queue_stats[stage_id] = stats
        self.__pydantic_fields_set__.add("queue_stats")

    def unset(self, field: str) -> None:
        """Drop an optional run-level field so `exclude_unset` serialization
        omits it — the model equivalent of `dict.pop(field, None)`. Clears the
        field from the model's set-fields (and resets it to None), so a run that
        never halted, or a resume that cleared a prior halt, writes no such key."""
        setattr(self, field, None)
        self.__pydantic_fields_set__.discard(field)

    def stage_record(self, stage_id: str) -> StageRecord | None:
        """This run's record for `stage_id`, or None if the run has no such
        stage."""
        return next((r for r in self.stages if r.stage_id == stage_id), None)

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
    *,
    run_id: str,
    project: str | None,
    workflow_version: str | None,
    run_bindings: dict[str, dict[str, Any]],
    input_bindings: dict[str, dict[str, Any]],
    limits: dict[str, int],
    offsets: dict[str, int],
) -> RunManifest:
    """The initial run manifest — every stage pending, status running. The single
    source of the run-manifest shape: every caller mints it here and persists it
    with write_manifest rather than hand-building the model, so the shape lives
    with the engine that later updates it.

    `project`/`workflow_version` are None for a subset run (run_subset) that was
    not told its logical identity — recorded honestly as None rather than a
    fabricated placeholder. A production run always supplies both. `queue_stats`
    and `dropped_columns` start empty and grow live as stages settle (the
    executor drains each stage's StageContribution into them)."""
    return RunManifest(
        run_id=run_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project,
        workflow_version=workflow_version,
        limit_overrides=limits,
        offset_overrides=offsets,
        run_bindings=run_bindings,
        input_bindings=input_bindings,
        queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stages=[StageRecord.pending(s) for s in ordered],
    )


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    """The single writer of run_dir/manifest.json. The initial write (prepare_run),
    every mid-run flush, and finalization all persist through here — dumping the
    typed model to the same `exclude_unset` JSON shape a reader parses back."""
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, default=str), encoding="utf-8"
    )


def load_manifest_model(run_dir: Path) -> RunManifest:
    """Parse a run's `manifest.json` off disk into a `RunManifest`, applying the
    model's normalization (a legacy scalar `halted_at` becomes a one-element
    list). Raises FileNotFoundError if the run has no manifest."""
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest at {manifest_path}")
    return RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
