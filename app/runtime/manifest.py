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

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.agent.usage import LlmUsage
from app.models import Stage, StageType
from app.core.run_status import RunStatus, StageStatus


class QueueStats(TypedDict):
    items_queued_total: int
    items_passed_through: int
    items_pending: int
    items_decided: int


class RowError(TypedDict):
    """`row` is a 0-based position."""

    row: int
    message: str


# The `.attrs` key a stage's output frame carries its StageContribution under.
CONTRIBUTION_ATTR = "stage_contribution"


class StageContribution(BaseModel):
    """What a stage handler contributes to the run manifest — never stage data."""

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
    """`traceback` is None for a row-generation error: there is no single exception to format."""

    type: str
    message: str
    traceback: str | None


class StageRecord(BaseModel):
    """The validation reports and row count carry no default: a pre-rename file must not parse as 0."""

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
        """`finished_at = None` on a non-running record is not a no-op: it marks the field set."""
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
        """Materializes `notes` on first use, so a stage that produced none omits the field."""
        if self.notes is None:
            self.notes = []
        self.notes.append(note)


class RunManifest(BaseModel):
    """A run-directory file artifact (`manifest.json`), not a document-store `PersistedModel`."""

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
        """A legacy scalar `halted_at` — a bare stage id — becomes a one-element list."""
        if isinstance(value, str):
            return [value]
        return value

    def settle_stage_records(self, records: list[StageRecord]) -> None:
        self.stage_records = records

    def record_dropped_columns(self, stage_id: str, columns: list[str]) -> None:
        """Marks the field set, so `exclude_unset` emits it even on a manifest loaded without it."""
        self.dropped_columns[stage_id] = columns
        self.__pydantic_fields_set__.add("dropped_columns")

    def record_human_review_queue_stats(self, stage_id: str, stats: QueueStats) -> None:
        self.human_review_queue_stats[stage_id] = stats

    def clear_halt(self) -> None:
        """Unsets `halted_at` so `exclude_unset` writes no key — no stale review banner on the run."""
        self.halted_at = None
        self.__pydantic_fields_set__.discard("halted_at")

    def find_stage_record(self, stage_id: str) -> StageRecord | None:
        return next((r for r in self.stage_records if r.stage_id == stage_id), None)

    def to_dict(self) -> dict[str, Any]:
        """Python mode: a status stays its enum member (a str enum, so json.dumps writes it bare)."""
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
    bust_cache: bool,
    is_test_run: bool,
) -> RunManifest:
    """`project`/`workflow_version` are None for a subset run with no logical identity — never faked."""
    return RunManifest(
        run_id=run_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project,
        workflow_version=workflow_version,
        limit_overrides=limits,
        offset_overrides=offsets,
        run_bindings=run_bindings,
        input_bindings=input_bindings,
        bust_cache=bust_cache,
        is_test_run=is_test_run,
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stage_records=[
            StageRecord.record_with_status(s, StageStatus.PENDING) for s in ordered
        ],
    )


def write_manifest(run_dir: Path, manifest: RunManifest) -> None:
    """The single writer of run_dir/manifest.json."""
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2, default=str), encoding="utf-8"
    )


def load_manifest_model(run_dir: Path) -> RunManifest:
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest at {manifest_path}")
    return RunManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
