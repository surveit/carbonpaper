"""Capturing a finished run: a restored run re-reads its sources, so they travel too."""

from __future__ import annotations

import shutil
from pathlib import Path

from app.core.files import compute_sha256
from app.core.run_status import RunStatus
from app.models.captured_run import CapturedInput, CapturedRun
from app.models.records.run_manifest import RunManifest
from app.models.run_manifest import StageInputRecord
from app.services.errors import RunCaptureRefused
from app.services.project import export_project_archive
from app.services.project_record import read_project_name
from app.services.run import read_run_manifest

CAPTURED_ARCHIVE = "project.zip"
CAPTURED_RECORD = "run.json"
CAPTURED_INPUTS = "inputs"

_CAPTURABLE_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


def capture_run(project_id: str, run_id: str, into: Path) -> CapturedRun:
    manifest = read_run_manifest(project_id, run_id)
    _validate_the_run_finished(manifest)
    inputs = _find_the_files_the_run_read(manifest)
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(export_project_archive(project_id))
    for entry in inputs:
        _copy_one_input(entry, into)
    captured = CapturedRun(
        project_name=read_project_name(project_id), run_id=run_id, inputs=inputs)
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


def _find_the_files_the_run_read(manifest: RunManifest) -> list[CapturedInput]:
    return [
        CapturedInput(stage_id=stage_id, file=file)
        for stage_id, record in sorted(manifest.input_bindings.items())
        for file in StageInputRecord.model_validate(record).files
    ]


def _copy_one_input(entry: CapturedInput, into: Path) -> None:
    source = Path(entry.file.path)
    if not source.is_file():
        raise RunCaptureRefused(
            f"stage '{entry.stage_id}' read {source}, which is no longer a file")
    target = into / CAPTURED_INPUTS / entry.stage_id / entry.file.filename
    if target.exists():
        raise RunCaptureRefused(
            f"stage '{entry.stage_id}' read two files named {entry.file.filename}; "
            f"the second, {source}, would overwrite the first under {CAPTURED_INPUTS}")
    _validate_the_bytes_still_hash_the_same(entry, source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def _validate_the_bytes_still_hash_the_same(entry: CapturedInput, source: Path) -> None:
    """Before the copy, so a file that moved on is never written into the capture."""
    digest = compute_sha256(source)
    if digest == entry.file.sha256:
        return
    raise RunCaptureRefused(
        f"stage '{entry.stage_id}': {source} now hashes to {digest}, not "
        f"the {entry.file.sha256} the run recorded — it changed since the run"
    )
