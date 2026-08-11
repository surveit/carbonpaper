"""Minting a run manifest, writing it to disk, and reading back the frames its
stages wrote — the shape, and the manifest read itself, are
`app.models.run_manifest`. The executor (`app.runtime.executor`) is its single
writer. Serialization is `exclude_unset`, so an optional field appears on disk
only once the run reaches the point that sets it.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import StageNotInRun, StageOutputMissing
from app.core.frames import read_frame_file
from app.core.run_status import RunStatus, StageStatus
from app.models import Stage
from app.models.run_manifest import RunManifest, StageRecord, read_run_manifest

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
    path = run_dir / "manifest.json"
    # Staged and swapped, never truncated in place: the run's background thread flushes
    # through here while the run page and the run tools read the same file, and a
    # truncating write leaves a window where a reader sees an empty file and raises
    # RunManifestNotJson. os.replace is atomic within ONE filesystem, so the temp file
    # is a sibling rather than somewhere under /tmp.
    staged = path.with_suffix(".json.writing")
    staged.write_text(
        json.dumps(manifest.to_dict(), indent=2, default=str), encoding="utf-8"
    )
    os.replace(staged, path)


def resolve_output_path(run_dir: Path, output_path: str | None) -> Path | None:
    if not output_path:
        return None
    resolved = (run_dir / output_path).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise StageOutputMissing(
            f"recorded output path '{output_path}' escapes run '{run_dir.name}'"
        )
    return resolved


def read_stage_output_frame(run_dir: Path, stage_id: str) -> pd.DataFrame:
    records = read_run_manifest(run_dir).stage_records
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
