"""The run manifest: its shape, minting one, storing it, and reading back the
frames its stages wrote. The executor (`app.runtime.executor`) is its single
writer. The per-stage pieces it embeds are `app.models.run_manifest`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.errors import (
    DocumentNotFound,
    RunNotFoundError,
    StageNotInRun,
    StageOutputMissing,
)
from app.core.frames import read_frame_file, read_frame_table
from app.core.json_types import JsonDict
from app.core.run_status import RunStatus, StageStatus
from app.models import WorkflowStage
from app.models.run_manifest import StageRecord
from app.models.records.run_manifest import (
    RunKind,
    RunManifest,
)

from .context import RunContext


# ─── The stored run manifest ─────────────────────────────────────────────────







def create_run_manifest(
    ordered: list[WorkflowStage],
    ctx: RunContext,
    *,
    run_id: str,
    project_id: str,
    workflow_version: str | None,
    input_bindings: dict[str, dict[str, Any]],
    kind: RunKind,
) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project_id, run_id),
        run_id=run_id,
        kind=kind,
        started_at=datetime.now().isoformat(timespec="seconds"),
        project=project_id,
        workflow_version=workflow_version,
        parameters=ctx.params,
        input_bindings=input_bindings,
        human_review_queue_stats={},
        dropped_columns={},
        status=RunStatus.RUNNING,
        stage_records=[
            StageRecord.record_with_status(s.stage, StageStatus.PENDING)
            for s in ordered
        ],
    )


def write_manifest(manifest: RunManifest) -> None:
    """The single writer of a run record."""
    manifest.save()


def read_run_manifest(project_id: str, run_id: str, expected_kind: RunKind) -> RunManifest:
    """Raises RunNotFoundError when unrecorded or recorded in a set other than `expected`."""
    try:
        manifest = RunManifest.load(RunManifest.compose_id(project_id, run_id))
    except DocumentNotFound as exc:
        raise RunNotFoundError(f"no run '{run_id}' in project '{project_id}'") from exc
    if manifest.kind != expected_kind:
        raise RunNotFoundError(
            f"run '{run_id}' of '{project_id}' is a {manifest.kind} run, "
            f"and a {expected_kind} run was asked for"
        )
    return manifest


@dataclass
class RunEntry:
    """One recorded run at BOTH levels; callers disagree on what unreadable means."""

    run_id: str
    project: str
    # None where the payload is not JSON: nothing then knows which set the run was in.
    kind: RunKind | None
    # `raw` is the stored payload, None when it is not even JSON. `manifest` is
    # that payload typed, None when this model rejects it (a run written before a
    # field was renamed). A caller needing ONE fact takes it off `raw`; one
    # needing the whole model waits for `manifest`.
    raw: JsonDict | None = None
    manifest: RunManifest | None = None


def list_run_entries(project_id: str, kind: RunKind) -> list[RunEntry]:
    """Project off the key, set off the record — each read where it is written down."""
    entries = [read_run_entry(project_id, _read_run_id(doc_id))
               for doc_id in RunManifest.list_ids(f"{project_id}/")]
    # A torn payload records no set; it stays listed. docs/run-manifest.md
    return sorted((e for e in entries if e.kind in (kind, None)), key=lambda e: e.run_id)


def read_run_entry(project_id: str, run_id: str) -> RunEntry:
    """Tolerant: `raw` is None for a payload that is not even JSON, `manifest` for one this model rejects."""
    raw = RunManifest.load_raw_or_none(RunManifest.compose_id(project_id, run_id))
    if raw is None:
        return RunEntry(run_id=run_id, project=project_id, kind=None)
    return _build_entry(project_id, run_id, _read_recorded_area(raw), raw)


def _build_entry(project_id: str, run_id: str, kind: RunKind | None,
                 raw: JsonDict) -> RunEntry:
    try:
        return RunEntry(run_id=run_id, project=project_id, kind=kind, raw=raw,
                        manifest=RunManifest.model_validate(raw))
    except ValidationError:
        return RunEntry(run_id=run_id, project=project_id, kind=kind, raw=raw)


def _read_recorded_area(raw: JsonDict) -> RunKind | None:
    recorded = raw.get("kind")
    return RunKind(recorded) if recorded in set(RunKind) else None


def _read_run_id(doc_id: str) -> str:
    """The id is `{project}/{run_id}` and a run id may hold a slash; a project id may not."""
    return doc_id.split("/", 1)[1]


def resolve_output_path(run_dir: Path, output_path: str | None) -> Path | None:
    if not output_path:
        return None
    resolved = (run_dir / output_path).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise StageOutputMissing(
            f"recorded output path '{output_path}' escapes run '{run_dir.name}'"
        )
    return resolved


def read_stage_output_frame(project_id: str, run_dir: Path, stage_id: str,
                            kind: RunKind) -> pd.DataFrame:
    return read_frame_file(_resolve_stage_output_file(project_id, run_dir, stage_id, kind))


def read_stage_output_frame_table(project_id: str, run_dir: Path, stage_id: str,
                                  kind: RunKind) -> pa.Table:
    return read_frame_table(_resolve_stage_output_file(project_id, run_dir, stage_id, kind))


def _resolve_stage_output_file(project_id: str, run_dir: Path, stage_id: str,
                               kind: RunKind) -> Path:
    records = read_run_manifest(project_id, run_dir.name, kind).stage_records
    record = _find_stage_record(records, run_dir, stage_id)
    path = resolve_output_path(run_dir, record.output_path)
    if path is None:
        raise StageOutputMissing(
            f"stage '{stage_id}' of run '{run_dir.name}' wrote no output "
            f"(its status is '{record.status}'), so it holds no values to read"
        )
    return path


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
