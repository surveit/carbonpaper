"""Package a finished run to restore elsewhere: its project archive, the files its
inputs read, and the record of what the archive does not carry."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.ids import ID
from app.core.run_status import RunStatus
from app.models.captured_run import CapturedDecision, CapturedRun
from app.models.records.review_decision import ReviewDecision
from app.models.records.run_manifest import RunManifest
from app.models.run_manifest import StageInputRecord
from app.models.run_parameters import RunParameters
from app.services.project import export_project_archive
from app.services.review import find_decisions_oldest_first
from app.services.run import read_run_manifest
from app.services.run_restore import CAPTURED_ARCHIVE, CAPTURED_INPUTS, CAPTURED_RECORD
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
    record = _record_what_the_workflow_does_not_carry(project_id, manifest)
    _validate_nothing_is_there_already(into)
    into.mkdir(parents=True, exist_ok=True)
    (into / CAPTURED_ARCHIVE).write_bytes(export_project_archive(project_id))
    (into / CAPTURED_RECORD).write_text(
        record.model_dump_json(indent=2), encoding="utf-8")
    for read in reads:
        _copy_one_input(read, into)


def _record_what_the_workflow_does_not_carry(
    project_id: str, manifest: RunManifest
) -> CapturedRun:
    parameters = manifest.parameters
    return CapturedRun(
        parameters=RunParameters(
            limits=parameters.limits,
            offsets=parameters.offsets,
            bust_cache=parameters.bust_cache,
        ),
        decisions=[
            _record_one_decision(decision)
            for decision in find_decisions_oldest_first(project_id)
        ],
    )


def _record_one_decision(decision: ReviewDecision) -> CapturedDecision:
    return CapturedDecision(
        stage_id=decision.stage_id,
        stage_fingerprint=decision.stage_fingerprint,
        input_fingerprint=decision.input_fingerprint,
        frozen_input=decision.frozen_input,
        verdict=decision.verdict,
        reviewed_values=decision.reviewed_values,
        review_notes=decision.review_notes,
        reviewer=decision.reviewer,
        reviewed_at=decision.reviewed_at,
    )


def _validate_nothing_is_there_already(into: Path) -> None:
    held = sorted(one.name for one in into.iterdir()) if into.is_dir() else []
    if held:
        raise RunCaptureRefused(
            f"{into} already holds {held} — a capture writes its own directory, so two "
            f"of them sharing one would read back as a single case")


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
