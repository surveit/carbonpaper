"""The run manifest: its shape, minting one, storing it, and reading back the
frames its stages wrote. The executor (`app.runtime.executor`) is its single
writer. The per-stage pieces it embeds are `app.models.run_manifest`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from dataclasses import dataclass

from pydantic import ValidationError

from app.core.errors import (
    DocumentNotFound,
    RunNotFoundError,
    StageNotInRun,
    StageOutputMissing,
)
from app.core.frames import read_frame_file
from app.core.json_types import JsonDict
from app.core.run_status import RunStatus, StageStatus
from app.models import WorkflowStage
from app.models.run_manifest import StageRecord
from app.models.records.run_manifest import (
    PRODUCTION_RUNS,
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
    area: str = PRODUCTION_RUNS,
) -> RunManifest:
    return RunManifest(
        id=RunManifest.compose_id(project_id, run_id, area),
        run_id=run_id,
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


def read_run_manifest(project_id: str, run_id: str, area: str = PRODUCTION_RUNS) -> RunManifest:
    """Raises RunNotFoundError when unrecorded, ValidationError on a bad payload."""
    try:
        return RunManifest.load(RunManifest.compose_id(project_id, run_id, area))
    except DocumentNotFound as exc:
        raise RunNotFoundError(f"no run '{run_id}' in project '{project_id}'") from exc


@dataclass
class RunEntry:
    """One recorded run at BOTH levels; callers disagree on what unreadable means."""

    run_id: str
    # The two id segments the listing already knew: which project's runs it read, and
    # which area under it. Carried so a reader spanning both can attribute an entry
    # without re-splitting the store key.
    project: str
    area: str
    # `raw` is the stored payload, None when it is not even JSON. `manifest` is
    # that payload typed, None when this model rejects it (a run written before a
    # field was renamed). A caller needing ONE fact takes it off `raw`; one
    # needing the whole model waits for `manifest`.
    raw: JsonDict | None = None
    manifest: RunManifest | None = None


def list_run_entries(project_id: str, area: str = PRODUCTION_RUNS) -> list[RunEntry]:
    """This project's runs in one area, oldest-first by id (a strftime stamp)."""
    prefix = f"{project_id}/{area}/"
    # Ids first, then each payload on its own: one unreadable record must not take
    # down the listing of every other run.
    entries = [
        _read_entry(doc_id, doc_id[len(prefix):], project_id, area)
        for doc_id in RunManifest.list_ids(prefix)
    ]
    return sorted(entries, key=lambda e: e.run_id)


def list_every_run_entry() -> list[RunEntry]:
    """Every recorded run. One outlives its project's working copy, and so does its cost."""
    entries = []
    for doc_id in RunManifest.list_ids():
        project_id, area, run_id = doc_id.split("/", 2)
        entries.append(_read_entry(doc_id, run_id, project_id, area))
    return sorted(entries, key=lambda e: (e.project, e.area, e.run_id))


def _read_entry(doc_id: str, run_id: str, project_id: str, area: str) -> RunEntry:
    raw = RunManifest.load_raw_or_none(doc_id)
    if raw is None:
        return RunEntry(run_id=run_id, project=project_id, area=area)
    try:
        return RunEntry(run_id=run_id, project=project_id, area=area, raw=raw,
                        manifest=RunManifest.model_validate(raw))
    except ValidationError:
        return RunEntry(run_id=run_id, project=project_id, area=area, raw=raw)


def resolve_output_path(run_dir: Path, output_path: str | None) -> Path | None:
    if not output_path:
        return None
    resolved = (run_dir / output_path).resolve()
    if not resolved.is_relative_to(run_dir.resolve()):
        raise StageOutputMissing(
            f"recorded output path '{output_path}' escapes run '{run_dir.name}'"
        )
    return resolved


def read_stage_output_frame(project_id: str, run_dir: Path, stage_id: str) -> pd.DataFrame:
    records = read_run_manifest(project_id, run_dir.name).stage_records
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
