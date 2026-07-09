"""Shared typed shapes for the runtime.

Two things live here because they cross module boundaries:
  - RunContext: the ambient state threaded through every stage handler.
  - StageRecord / RunManifest: the on-disk run-manifest structure, WRITTEN by the
    runner and READ back by the web layer — a single typed contract for both ends.

Every value in the manifest types is JSON-native (it is `json.dumps`-ed to
manifest.json and reloaded), so they carry no `Any`: known scalars where the
shape is fixed, and `object` only for the genuinely dynamic per-stage stat blobs.
"""
from __future__ import annotations

from pathlib import Path
from typing import TypedDict

from app.models import Stage
from app.runtime.llm import BackendStatus
from app.runtime.validation import ValidationReportDict

# queue_stats maps a stage id → a small stats dict whose values are genuinely
# mixed (ints, strings). `object` is the honest element type: heterogeneous,
# read back out with isinstance narrowing.
_PerStage = dict[str, dict[str, object]]


class RunContext(TypedDict, total=False):
    """Ambient state handed to every stage handler. total=False because a handler
    reads a fixed subset and two keys (queue_stats, llm_backend) are populated
    lazily by the handlers that need them."""
    repo_root: Path
    run_dir: Path
    project_dir: Path
    limits: dict[str, int]
    offsets: dict[str, int]
    queue_stats: _PerStage
    llm_backend: dict[str, BackendStatus]
    # Marks an in-memory scratch preview (see app.runtime.preview) so a handler
    # could branch on it; never persisted.
    _scratch_preview: bool


class _StageRecordRequired(TypedDict):
    # stage_id is always present (the runner writes it for every stage); split
    # out as Required so callers can key a dict on it without an Optional.
    stage_id: str


class StageRecord(_StageRecordRequired, total=False):
    """One stage's entry in a run manifest's `stages` list."""
    type: str  # StageType serialises to its plain value string
    name: str
    status: str
    input_validation: list[ValidationReportDict]
    output_validation: ValidationReportDict | None
    elapsed_ms: int
    rows: int
    error: dict[str, str] | None
    started_at: str | None
    finished_at: str | None
    queue_path: str
    output_path: str
    notes: list[str]


class RunManifest(TypedDict, total=False):
    """A run's manifest.json: written incrementally by the runner as stages
    execute, read by the web layer to render the run page."""
    run_id: str
    started_at: str
    project: str
    workflow_version: str
    limit_overrides: dict[str, int]
    offset_overrides: dict[str, int]
    status: str
    stages: list[StageRecord]
    queue_stats: _PerStage
    updated_at: str
    finished_at: str
    halted_at: str
    resumed_at: str


class RunPrep(TypedDict):
    """The bundle prepare_run() hands to run_prepared() — a set-up-but-not-yet-
    executed run."""
    run_id: str
    run_dir: Path
    ctx: RunContext
    ordered: list[Stage]
    manifest: RunManifest
