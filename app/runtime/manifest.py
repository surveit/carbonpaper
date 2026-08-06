"""Minting a run manifest, and its on-disk file IO — the shape itself is
`app.models.run_manifest`. The executor (`app.runtime.executor`) is its single
writer; every other layer reads it back. Serialization is `exclude_unset`, so an
optional field appears on disk only once the run reaches the point that sets it.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import StageNotInRun, StageOutputMissing
from app.core.frames import read_frame_file
from app.core.run_status import RunStatus, StageStatus
from app.models import Stage
from app.models.run_manifest import RunManifest, StageRecord

from .context import RunContext


# The `.attrs` key a stage's output frame carries its StageContribution under.
CONTRIBUTION_ATTR = "stage_contribution"


def create_run_manifest(
    ordered: list[Stage],
    ctx: RunContext,
    *,
    run_id: str,
    project: str | None,
    workflow_version: str | None,
    input_bindings: dict[str, dict[str, Any]],
) -> RunManifest:
    """The initial run manifest — every stage pending, status running. The single
    source of the run-manifest shape: every caller mints it here and persists it
    with write_manifest rather than hand-building the model, so the shape lives
    with the engine that later updates it.

    Everything the caller DECIDED is `ctx.params`, recorded verbatim — the same
    object the engine executes against, so a caller cannot set one and record
    another. What this takes besides is what the run turns out to BE: its identity,
    and the preflight provenance of its bound inputs.

    `project`/`workflow_version` are None for a subset run (run_subset) that was
    not told its logical identity — recorded honestly as None rather than a
    fabricated placeholder. A production run always supplies both.
    `human_review_queue_stats` and `dropped_columns` start empty
    and grow live as stages settle (the executor drains each stage's
    StageContribution into them)."""
    return RunManifest(
        run_id=run_id,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project,
        workflow_version=workflow_version,
        parameters=ctx.params,
        input_bindings=input_bindings,
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
