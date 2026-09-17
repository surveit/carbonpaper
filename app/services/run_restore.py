"""Restoring a captured run: a fresh project, its input files, and the re-run."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.core.files import ProjectFile, compute_sha256, save_upload
from app.core.ids import ID
from app.core.run_status import RunStatus
from app.models import Workflow
from app.models.captured_run import (
    CAPTURED_ARCHIVE,
    CAPTURED_INPUTS,
    CAPTURED_RECORD,
    CapturedInput,
    CapturedRun,
)
from app.models.records.run_manifest import RunManifest
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.stage_base import StageType
from app.models.workflow_stage import WorkflowStage
from app.services.errors import ProjectArchiveRejected, RunRestoreRefused
from app.services.project import import_project_archive
from app.services.run import execute, read_run_manifest
from app.services.stage_cache_transfer import CacheImportReport
from app.services.uploads import resolve_files_binding
from app.services.versioning import load_version_stages, resolve_version_id

_RESTORED_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


class RestoredRun(BaseModel):
    project_id: ID
    run_id: ID


def restore_run(from_dir: Path) -> RestoredRun:
    captured = _read_the_captured_record(from_dir)
    project_id = _import_the_captured_project_id(from_dir)
    _validate_no_stage_queues_rows_for_review(project_id)
    bindings = _bind_the_files_the_run_read(project_id, from_dir, captured.inputs)
    run_id = str(execute(project_id, bindings=bindings)["run_id"])
    _validate_the_restored_run_finished(captured, read_run_manifest(project_id, run_id))
    return RestoredRun(project_id=project_id, run_id=run_id)


def _read_the_captured_record(from_dir: Path) -> CapturedRun:
    record = from_dir / CAPTURED_RECORD
    if not record.is_file():
        raise RunRestoreRefused(
            f"no {CAPTURED_RECORD} at {record} — a captured run carries its own record "
            "beside the archive")
    try:
        return CapturedRun.model_validate_json(record.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise RunRestoreRefused(f"{record} is not a captured run: {exc}") from exc


def _import_the_captured_project_id(from_dir: Path) -> ID:
    archive = from_dir / CAPTURED_ARCHIVE
    if not archive.is_file():
        raise RunRestoreRefused(
            f"no {CAPTURED_ARCHIVE} at {archive} — there is no project to restore")
    try:
        report = import_project_archive(archive.read_bytes())
    except ProjectArchiveRejected as exc:
        raise RunRestoreRefused(f"{archive} did not import: {exc}") from exc
    _validate_the_cache_came_with_it(archive, report.cache)
    return report.project_id


def _validate_the_cache_came_with_it(archive: Path, cache: CacheImportReport | None) -> None:
    """Without it the re-run pays for every model call the captured run already paid for."""
    if cache is None:
        raise RunRestoreRefused(f"{archive} carries no stage cache")
    stored = cache.written + cache.already_stored
    if stored and not cache.reachable:
        raise RunRestoreRefused(
            f"{archive} carries {stored} cache entries and this workspace can read none "
            "of them: the stages they were computed for have moved since the capture")


def _validate_no_stage_queues_rows_for_review(project_id: ID) -> None:
    queueing = [
        placed.id for placed in _read_the_workflow_the_run_will_take(project_id)
        if placed.stage.type is StageType.human_review_queue
    ]
    if queueing:
        raise RunRestoreRefused(
            f"stage '{queueing[0]}' queues rows for human review, so the restored run "
            "would halt waiting for a reviewer rather than finish")


def _read_the_workflow_the_run_will_take(project_id: ID) -> Sequence[WorkflowStage]:
    version_id = resolve_version_id(project_id, None)
    workflow = Workflow(stages=load_version_stages(project_id, version_id))
    return workflow.list_workflow_stages()


def _bind_the_files_the_run_read(
    project_id: ID, from_dir: Path, inputs: Sequence[CapturedInput]
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    staged_file_ids: dict[StageId, list[ID]] = {}
    for entry in inputs:
        record = _save_one_captured_input(project_id, from_dir, entry)
        staged_file_ids.setdefault(entry.stage_id, []).append(record.id)
    return {
        stage_id: resolve_files_binding(project_id, file_ids)
        for stage_id, file_ids in staged_file_ids.items()
    }


def _save_one_captured_input(
    project_id: ID, from_dir: Path, entry: CapturedInput
) -> ProjectFile:
    path = from_dir / CAPTURED_INPUTS / entry.stage_id / entry.filename
    _validate_the_bytes_are_what_was_captured(entry, path)
    with path.open("rb") as handle:
        return save_upload(entry.filename, handle, project_id=project_id)


def _validate_the_bytes_are_what_was_captured(entry: CapturedInput, path: Path) -> None:
    if not path.is_file():
        raise RunRestoreRefused(
            f"stage '{entry.stage_id}' read '{entry.filename}', which the capture does "
            f"not hold at {path}")
    weighed = path.stat().st_size
    if weighed != entry.bytes:
        raise RunRestoreRefused(
            f"stage '{entry.stage_id}': {path} weighs {weighed} bytes, not the "
            f"{entry.bytes} the capture recorded")
    digest = compute_sha256(path)
    if digest != entry.sha256:
        raise RunRestoreRefused(
            f"stage '{entry.stage_id}': {path} hashes to {digest}, not the "
            f"{entry.sha256} the capture recorded")


def _validate_the_restored_run_finished(
    captured: CapturedRun, manifest: RunManifest
) -> None:
    if manifest.status in _RESTORED_STATUSES:
        return
    raise RunRestoreRefused(
        f"the restored run of '{captured.run_id}' is {manifest.status}, not "
        f"{' or '.join(_RESTORED_STATUSES)}: " + "; ".join(_quote_the_failures(manifest))
    )


def _quote_the_failures(manifest: RunManifest) -> list[str]:
    quoted = [
        f"stage '{record.stage_id}' {record.status}: {record.error.message}"
        for record in manifest.stage_records if record.error is not None
    ]
    return quoted or [f"no stage recorded an error, and it halted at {manifest.halted_at}"]
