"""Restoring a captured run: a fresh project, its input files, and the re-run."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ValidationError

from app.core.files import ProjectFile, save_upload
from app.core.ids import ID
from app.core.run_status import RunStatus
from app.models.captured_run import CapturedRun
from app.models.records.run_manifest import RunManifest
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.services.errors import ProjectArchiveRejected, RunRestoreRefused
from app.services.project import WorkflowFile, import_project_archive, read_archive_workflow
from app.services.run import execute, read_run_manifest
from app.services.stage_cache_transfer import CacheImportReport
from app.services.uploads import resolve_files_binding
from app.services.versioning import resolve_version_id

# The capture writes this layout and a restore reads it, so it is named with the reader.
CAPTURED_ARCHIVE = "project.zip"
CAPTURED_INPUTS = "inputs"
CAPTURED_RECORD = "run.json"

_RESTORED_STATUSES = (RunStatus.OK, RunStatus.WARNINGS)


class RestoredRun(BaseModel):
    project_id: ID
    run_id: ID


def restore_run(from_dir: Path) -> RestoredRun:
    """Everything the capture alone can settle is refused before a project is written."""
    archive = _find_the_captured_archive(from_dir)
    raw = archive.read_bytes()
    workflow = _read_the_bundled_workflow(archive, raw)
    captured = _read_the_captured_record(from_dir)
    _validate_every_input_directory_names_a_stage(from_dir, workflow)
    project_id = _import_the_captured_project_id(archive, raw)
    bindings = _bind_the_files_the_run_read(project_id, from_dir)
    version_id = resolve_version_id(project_id, None)
    run_id = _execute_as_captured(project_id, version_id, bindings, captured)
    _validate_the_restored_run_finished(read_run_manifest(project_id, run_id))
    return RestoredRun(project_id=project_id, run_id=run_id)


def _execute_as_captured(
    project_id: ID,
    version_id: str,
    bindings: dict[StageId, TypeUnsafeUserStageConfigOverride],
    captured: CapturedRun,
) -> str:
    parameters = captured.parameters
    return str(execute(
        project_id,
        version_id=version_id,
        bindings=bindings,
        limits=dict(parameters.limits),
        offsets=dict(parameters.offsets),
        bust_cache=parameters.bust_cache,
    )["run_id"])


def _read_the_captured_record(from_dir: Path) -> CapturedRun:
    record = from_dir / CAPTURED_RECORD
    if not record.is_file():
        raise RunRestoreRefused(
            f"no {CAPTURED_RECORD} at {record} — the row window the captured run "
            "used is recorded nowhere else")
    try:
        return CapturedRun.model_validate_json(record.read_text(encoding="utf-8"))
    except ValidationError as exc:
        raise RunRestoreRefused(f"{record} is not a captured run: {exc}") from exc


def _find_the_captured_archive(from_dir: Path) -> Path:
    archive = from_dir / CAPTURED_ARCHIVE
    if not archive.is_file():
        raise RunRestoreRefused(
            f"no {CAPTURED_ARCHIVE} at {archive} — there is no project to restore")
    return archive


def _read_the_bundled_workflow(archive: Path, raw: bytes) -> WorkflowFile:
    try:
        return read_archive_workflow(raw)
    except ProjectArchiveRejected as exc:
        raise RunRestoreRefused(f"{archive} did not import: {exc}") from exc


def _validate_every_input_directory_names_a_stage(
    from_dir: Path, workflow: WorkflowFile
) -> None:
    known = {stage.id for stage in workflow.stages}
    for directory in _find_the_captured_input_directories(from_dir):
        if directory.name not in known:
            raise RunRestoreRefused(
                f"the capture holds {CAPTURED_INPUTS}/{directory.name}, and the restored "
                f"workflow has no stage '{directory.name}' to read those files")


def _import_the_captured_project_id(archive: Path, raw: bytes) -> ID:
    try:
        report = import_project_archive(raw)
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


def _bind_the_files_the_run_read(
    project_id: ID, from_dir: Path
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    return {
        directory.name: _bind_the_files_of_one_stage(project_id, directory)
        for directory in _find_the_captured_input_directories(from_dir)
    }


def _find_the_captured_input_directories(from_dir: Path) -> list[Path]:
    inputs = from_dir / CAPTURED_INPUTS
    if not inputs.is_dir():
        return []
    return sorted(path for path in inputs.iterdir() if path.is_dir())


def _bind_the_files_of_one_stage(
    project_id: ID, directory: Path
) -> TypeUnsafeUserStageConfigOverride:
    saved = [
        _save_one_captured_input(project_id, path)
        for path in sorted(directory.iterdir()) if path.is_file()
    ]
    return resolve_files_binding(project_id, [record.id for record in saved])


def _save_one_captured_input(project_id: ID, path: Path) -> ProjectFile:
    with path.open("rb") as handle:
        return save_upload(path.name, handle, project_id=project_id)


def _validate_the_restored_run_finished(manifest: RunManifest) -> None:
    if manifest.status in _RESTORED_STATUSES:
        return
    raise RunRestoreRefused(
        f"the restored run is {manifest.status}, not "
        f"{' or '.join(_RESTORED_STATUSES)}: " + "; ".join(_quote_the_failures(manifest))
    )


def _quote_the_failures(manifest: RunManifest) -> list[str]:
    quoted = [
        f"stage '{record.stage_id}' {record.status}: {record.error.message}"
        for record in manifest.stage_records if record.error is not None
    ]
    return quoted or [f"no stage recorded an error, and it halted at {manifest.halted_at}"]
