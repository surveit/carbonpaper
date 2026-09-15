from __future__ import annotations

import subprocess
from collections import Counter
from datetime import datetime
from pathlib import Path

from app.core.files import ProjectFile
from app.core.run_status import RunStatus
from app.models.claims import StageOutputCellCitation
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.run_manifest import InputBinding, read_input_bindings
from app.models.stages.human_review_queue import HumanReviewQueueStage
from app.services.project import export_project_archive
from app.services.run import read_run_manifest
from app.services.versioning import find_latest_version_id, load_version_stages
from evals.runs.recipe import (
    ARCHIVE_FILE,
    RECIPE_FILE,
    CapturedFrom,
    RecipeFigure,
    RecipeInput,
    RunRecipe,
    SuppliedLocation,
    write_recipe,
)

_CAPTURED_STATUSES = (RunStatus.OK, RunStatus.WARNINGS, RunStatus.AWAITING_REVIEW)
_ENDED_PAST_EVERY_QUEUE = (RunStatus.OK, RunStatus.WARNINGS)
_WORKFLOW_SOURCE = "workflow"


class CaptureRefused(Exception):
    pass


def capture_run(
    project_id: str, run_id: str, runs_root: Path, *, repo_root: Path, replace: bool
) -> Path:
    run_dir = runs_root / run_id
    _validate_may_write_to(run_dir, replace=replace)
    manifest = read_run_manifest(project_id, run_id)
    _validate_run_is_capturable(manifest)
    workflow_version = _require_latest_version(project_id, manifest)
    _validate_review_decisions_can_travel(project_id, workflow_version, manifest)
    inputs = [_record_input(binding) for binding in read_input_bindings(manifest.to_dict())]
    figures = _record_figures(run_id)
    recipe = RunRecipe(
        workflow_run_id=run_id,
        captured=_record_captured_from(project_id, workflow_version, repo_root),
        inputs=inputs,
        limits=dict(manifest.parameters.limits),
        offsets=dict(manifest.parameters.offsets),
        ends=manifest.status,
        figures=figures,
    )
    _write_saved_run(run_dir, export_project_archive(project_id), recipe)
    return run_dir


def _validate_may_write_to(run_dir: Path, *, replace: bool) -> None:
    if run_dir.exists() and not replace:
        raise CaptureRefused(
            f"{run_dir} already holds a saved run; capture with replace to rewrite its "
            f"{ARCHIVE_FILE} and {RECIPE_FILE}"
        )


def _validate_run_is_capturable(manifest: RunManifest) -> None:
    if manifest.status not in _CAPTURED_STATUSES:
        raise CaptureRefused(
            f"run '{manifest.run_id}' has status '{manifest.status}'; capture takes only a run "
            f"whose status is one of {', '.join(_CAPTURED_STATUSES)}"
        )
    if manifest.parameters.is_test_run:
        raise CaptureRefused(
            f"run '{manifest.run_id}' is a test run; capture a production run of the workflow"
        )


def _require_latest_version(project_id: str, manifest: RunManifest) -> str:
    latest = find_latest_version_id(project_id)
    if manifest.workflow_version is None or manifest.workflow_version != latest:
        raise CaptureRefused(
            f"run '{manifest.run_id}' ran workflow version '{manifest.workflow_version}', but "
            f"the latest version of project '{project_id}' is '{latest}', and the archive "
            "exports the latest; capture a run of the latest version"
        )
    return manifest.workflow_version


def _validate_review_decisions_can_travel(
    project_id: str, workflow_version: str, manifest: RunManifest
) -> None:
    if manifest.status not in _ENDED_PAST_EVERY_QUEUE:
        return
    uncached = sorted(
        stage.id
        for stage in load_version_stages(project_id, workflow_version)
        if isinstance(stage, HumanReviewQueueStage) and not stage.cache
    )
    if uncached:
        raise CaptureRefused(
            f"run '{manifest.run_id}' ended '{manifest.status}' past review queue stage(s) "
            f"{uncached}, which do not cache: a saved run carries no review decisions, only "
            "the stage cache those stages never read, so a rebuild would halt for review"
        )


def _record_input(binding: InputBinding) -> RecipeInput:
    if binding.source == _WORKFLOW_SOURCE:
        raise CaptureRefused(
            f"stage '{binding.stage_id}' read {binding.path}, a path the workflow names; that "
            "file cannot follow a saved run, so capture a run that binds an uploaded file"
        )
    record = _load_file_record(binding)
    if binding.bytes is None or binding.sha256 is None:
        raise CaptureRefused(
            f"stage '{binding.stage_id}' read '{record.filename}', but the run recorded no "
            "size or sha256 for it, so a rebuild could not check the file it is given"
        )
    return RecipeInput(
        stage_id=binding.stage_id,
        filename=record.filename,
        bytes=binding.bytes,
        sha256=binding.sha256,
        at=SuppliedLocation(),
    )


def _load_file_record(binding: InputBinding) -> ProjectFile:
    if binding.file_id is None:
        raise CaptureRefused(
            f"stage '{binding.stage_id}' read '{binding.filename}' from {binding.path}, which is "
            "not a file uploaded to the project; capture a run that binds an uploaded file"
        )
    record = ProjectFile.load_or_none(binding.file_id)
    if record is None:
        raise CaptureRefused(
            f"stage '{binding.stage_id}' read '{binding.filename}', stored as file "
            f"'{binding.file_id}', whose record is gone; capture a run over a file the "
            "project still holds"
        )
    return record


def _record_captured_from(project_id: str, workflow_version: str, repo_root: Path) -> CapturedFrom:
    return CapturedFrom(
        project_id=project_id,
        workflow_version=workflow_version,
        captured_at=datetime.now().isoformat(timespec="seconds"),
        code_commit=_read_head_commit(repo_root),
    )


def _read_head_commit(repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, check=True
    )
    return completed.stdout.decode("utf-8").strip()


def _record_figures(run_id: str) -> list[RecipeFigure]:
    figures = [
        RecipeFigure(
            slug=output.slug,
            stage_id=output.citation.stage_id,
            value=output.citation.value,
            claimable=output.shape_id is not None,
        )
        for output in WorkflowOutput.list()
        if isinstance(output.citation, StageOutputCellCitation)
        and output.citation.run_id == run_id
    ]
    _validate_each_slug_names_one_figure(run_id, figures)
    return sorted(figures, key=lambda figure: figure.slug)


def _validate_each_slug_names_one_figure(run_id: str, figures: list[RecipeFigure]) -> None:
    counts = Counter(figure.slug for figure in figures)
    repeated = sorted(slug for slug, count in counts.items() if count > 1)
    if repeated:
        raise CaptureRefused(
            f"run '{run_id}' published more than one figure under slug(s) {repeated}; a "
            "rebuild matches figures by slug, so each slug must name one figure"
        )


def _write_saved_run(run_dir: Path, archive: bytes, recipe: RunRecipe) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / ARCHIVE_FILE).write_bytes(archive)
    write_recipe(run_dir, recipe)
