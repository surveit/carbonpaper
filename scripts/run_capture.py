"""Package a finished run to restore elsewhere: its project archive and input files."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.ids import ID
from app.core.run_status import RunStatus
from app.models.records.run_manifest import RunManifest
from app.models.run_manifest import StageInputRecord
from app.services.project import export_project_archive
from app.services.run import read_run_manifest
from app.services.run_restore import CAPTURED_ARCHIVE, CAPTURED_INPUTS
from scripts.errors import RunCaptureRefused

_CAPTURABLE_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


@dataclass(frozen=True)
class _FileTheRunRead:
    stage_id: ID
    filename: str
    source: Path


def capture_run(project_id: str, run_id: str, into: Path) -> None:
    manifest = read_run_manifest(project_id, run_id)
    _validate_the_run_finished(manifest)
    reads = _find_the_files_the_run_read(manifest)
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(export_project_archive(project_id))
    for read in reads:
        _copy_one_input(read, into)


def _validate_the_run_finished(manifest: RunManifest) -> None:
    if manifest.status in _CAPTURABLE_STATUSES:
        return
    finished = " or ".join(_CAPTURABLE_STATUSES)
    raise RunCaptureRefused(
        f"run '{manifest.run_id}' is {manifest.status}; only a run that "
        f"finished {finished} can be captured"
    )


def _find_the_files_the_run_read(manifest: RunManifest) -> list[_FileTheRunRead]:
    return [
        _FileTheRunRead(stage_id=stage_id, filename=file.filename, source=Path(file.path))
        for stage_id, record in sorted(manifest.input_bindings.items())
        for file in StageInputRecord.model_validate(record).files
    ]


def _copy_one_input(read: _FileTheRunRead, into: Path) -> None:
    if not read.source.is_file():
        raise RunCaptureRefused(
            f"stage '{read.stage_id}' read {read.source}, which is no longer a file")
    target = into / CAPTURED_INPUTS / read.stage_id / read.filename
    if target.exists():
        raise RunCaptureRefused(
            f"stage '{read.stage_id}' read two files named {read.filename}; the second, "
            f"{read.source}, would overwrite the first under {CAPTURED_INPUTS}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(read.source, target)
