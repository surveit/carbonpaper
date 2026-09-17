"""Package a finished run to restore elsewhere: its project archive and input files."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.files import compute_sha256
from app.core.run_status import RunStatus
from app.models.captured_run import (
    CAPTURED_ARCHIVE,
    CAPTURED_INPUTS,
    CAPTURED_RECORD,
    CapturedInput,
    CapturedRun,
)
from app.models.records.run_manifest import RunManifest
from app.models.run_manifest import StageInputRecord
from app.services.project import export_project_archive
from app.services.project_record import read_project_name
from app.services.run import read_run_manifest
from scripts.errors import RunCaptureRefused

_CAPTURABLE_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


@dataclass(frozen=True)
class _FileTheRunRead:
    """`source` is where it sits on this machine; `entry` is what the capture records."""

    entry: CapturedInput
    source: Path


def capture_run(project_id: str, run_id: str, into: Path) -> CapturedRun:
    manifest = read_run_manifest(project_id, run_id)
    _validate_the_run_finished(manifest)
    reads = _find_the_files_the_run_read(manifest)
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(export_project_archive(project_id))
    for read in reads:
        _copy_one_input(read, into)
    captured = CapturedRun(
        project_name=read_project_name(project_id), run_id=run_id,
        inputs=[read.entry for read in reads])
    (into / CAPTURED_RECORD).write_text(
        captured.model_dump_json(indent=2), encoding="utf-8")
    return captured


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
        _FileTheRunRead(
            entry=CapturedInput(stage_id=stage_id, filename=file.filename,
                                sha256=file.sha256, bytes=file.bytes),
            source=Path(file.path),
        )
        for stage_id, record in sorted(manifest.input_bindings.items())
        for file in StageInputRecord.model_validate(record).files
    ]


def _copy_one_input(read: _FileTheRunRead, into: Path) -> None:
    entry, source = read.entry, read.source
    if not source.is_file():
        raise RunCaptureRefused(
            f"stage '{entry.stage_id}' read {source}, which is no longer a file")
    target = into / CAPTURED_INPUTS / entry.stage_id / entry.filename
    if target.exists():
        raise RunCaptureRefused(
            f"stage '{entry.stage_id}' read two files named {entry.filename}; "
            f"the second, {source}, would overwrite the first under {CAPTURED_INPUTS}")
    _validate_the_bytes_still_hash_the_same(read)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _validate_the_bytes_still_hash_the_same(read: _FileTheRunRead) -> None:
    """Before the copy, so a file that moved on is never written into the capture."""
    digest = compute_sha256(read.source)
    if digest == read.entry.sha256:
        return
    raise RunCaptureRefused(
        f"stage '{read.entry.stage_id}': {read.source} now hashes to {digest}, not "
        f"the {read.entry.sha256} the run recorded — it changed since the run"
    )
