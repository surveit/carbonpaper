from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import NamedTuple

from pydantic import BaseModel

from app.core.files import save_upload
from app.core.json_types import JsonScalar
from app.core.run_status import RunStatus
from app.models.claims import StageOutputCellCitation
from app.models.records.run_manifest import RunManifest
from app.models.records.workflow_output import WorkflowOutput
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.stage_base import StageType
from app.runtime import options
from app.services.project import ProjectImportReport, import_project_archive
from app.services.run import execute, read_run_manifest
from app.services.uploads import resolve_files_binding
from evals.runs.recipe import (
    ARCHIVE_FILE,
    RecipeFigure,
    RecipeInput,
    RepoPathLocation,
    RunRecipe,
    read_recipe,
)


class RunRefused(Exception):
    def __init__(self, reasons: list[str]) -> None:
        super().__init__("\n".join(reasons))
        self.reasons = reasons


class RebuiltRun(BaseModel):
    project_id: str
    run_id: str


class _VerifiedInput(NamedTuple):
    recorded: RecipeInput
    content: bytes


def rebuild_run(run_dir: Path, *, repo_root: Path, inputs_dir: Path | None) -> RebuiltRun:
    recipe = read_recipe(run_dir)
    inputs = _read_verified_inputs(recipe.inputs, repo_root, inputs_dir)
    project_id = _import_archive(run_dir / ARCHIVE_FILE)
    bindings = _store_and_bind_inputs(project_id, inputs)
    with _switch_off_models():
        returned = execute(
            project_id, bindings=bindings, limits=recipe.limits, offsets=recipe.offsets
        )
    run_id = str(returned["run_id"])
    _validate_rebuild_matches_recipe(recipe, read_run_manifest(project_id, run_id))
    return RebuiltRun(project_id=project_id, run_id=run_id)


def find_model_spend(manifest: RunManifest) -> list[str]:
    return [
        record.stage_id
        for record in manifest.stage_records
        if record.type == StageType.llm_transform
        and record.llm_usage is not None
        and record.llm_usage.calls > 0
    ]


def _read_verified_inputs(
    recorded_inputs: list[RecipeInput], repo_root: Path, inputs_dir: Path | None
) -> list[_VerifiedInput]:
    verified: list[_VerifiedInput] = []
    reasons: list[str] = []
    for recorded in recorded_inputs:
        try:
            verified.append(_read_verified_input(recorded, repo_root, inputs_dir))
        except RunRefused as refused:
            reasons += refused.reasons
    if reasons:
        raise RunRefused(reasons)
    return verified


def _read_verified_input(
    recorded: RecipeInput, repo_root: Path, inputs_dir: Path | None
) -> _VerifiedInput:
    path = _locate_input(recorded, repo_root, inputs_dir)
    if not path.is_file():
        raise RunRefused([f"{_describe_input(recorded)} is not at {path}, where rebuild looked"])
    content = path.read_bytes()
    mismatches = _find_content_mismatches(recorded, path, content)
    if mismatches:
        raise RunRefused(mismatches)
    return _VerifiedInput(recorded=recorded, content=content)


def _locate_input(recorded: RecipeInput, repo_root: Path, inputs_dir: Path | None) -> Path:
    if isinstance(recorded.at, RepoPathLocation):
        return _locate_under_repo_root(recorded, recorded.at, repo_root)
    if inputs_dir is None:
        raise RunRefused([
            f"{_describe_input(recorded)} is supplied at rebuild, but no inputs folder was "
            "given to look for it in"
        ])
    return inputs_dir / recorded.filename


def _locate_under_repo_root(
    recorded: RecipeInput, location: RepoPathLocation, repo_root: Path
) -> Path:
    root = repo_root.resolve()
    path = (root / location.path).resolve()
    if not path.is_relative_to(root):
        raise RunRefused([
            f"{_describe_input(recorded)} names repo path '{location.path}', which resolves "
            f"to {path}, outside the repo root {root}"
        ])
    return path


def _find_content_mismatches(recorded: RecipeInput, path: Path, content: bytes) -> list[str]:
    mismatches: list[str] = []
    if len(content) != recorded.bytes:
        mismatches.append(
            f"{_describe_input(recorded)} at {path} holds {len(content)} bytes, but the "
            f"recipe recorded {recorded.bytes} bytes"
        )
    digest = hashlib.sha256(content).hexdigest()
    if digest != recorded.sha256:
        mismatches.append(
            f"{_describe_input(recorded)} at {path} has sha256 {digest}, but the recipe "
            f"recorded sha256 {recorded.sha256}"
        )
    return mismatches


def _describe_input(recorded: RecipeInput) -> str:
    return f"input '{recorded.filename}' of stage '{recorded.stage_id}'"


def _import_archive(archive: Path) -> str:
    report = import_project_archive(archive.read_bytes())
    _validate_cache_fits_workflow(report)
    return report.project_id


def _validate_cache_fits_workflow(report: ProjectImportReport) -> None:
    cache = report.cache
    if cache is None:
        return
    entries = cache.written + cache.already_stored
    if entries > 0 and cache.reachable == 0:
        raise RunRefused([
            f"the archive's stage cache holds {entries} entries and its workflow reads none of "
            "them: the cache no longer fits the workflow, so capture the run again"
        ])


def _store_and_bind_inputs(
    project_id: str, inputs: list[_VerifiedInput]
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    file_ids: dict[StageId, list[str]] = {}
    for verified in inputs:
        stored = save_upload(verified.recorded.filename, BytesIO(verified.content), project_id)
        file_ids.setdefault(verified.recorded.stage_id, []).append(stored.id)
    return {stage_id: resolve_files_binding(project_id, ids) for stage_id, ids in file_ids.items()}


@contextmanager
def _switch_off_models() -> Iterator[None]:
    available = options.agent_available
    options.agent_available = _report_no_agent
    try:
        yield
    finally:
        options.agent_available = available


def _report_no_agent() -> bool:
    return False


def _validate_rebuild_matches_recipe(recipe: RunRecipe, manifest: RunManifest) -> None:
    reasons = [
        *_find_status_difference(recipe.ends, manifest),
        *_describe_model_spend(manifest),
        *_find_figure_differences(recipe.figures, _read_published_figures(manifest.run_id)),
    ]
    if reasons:
        raise RunRefused(reasons)


def _find_status_difference(ends: RunStatus, manifest: RunManifest) -> list[str]:
    if manifest.status == ends:
        return []
    return [f"the rebuilt run ended '{manifest.status}', but the recipe recorded '{ends}'"]


def _describe_model_spend(manifest: RunManifest) -> list[str]:
    return [
        f"stage '{stage_id}' called a model while rebuilding, which a rebuild must never do"
        for stage_id in find_model_spend(manifest)
    ]


def _read_published_figures(run_id: str) -> dict[str, list[JsonScalar]]:
    published: dict[str, list[JsonScalar]] = {}
    for output in WorkflowOutput.list():
        citation = output.citation
        if isinstance(citation, StageOutputCellCitation) and citation.run_id == run_id:
            published.setdefault(output.slug, []).append(citation.value)
    return published


def _find_figure_differences(
    recorded: list[RecipeFigure], published: dict[str, list[JsonScalar]]
) -> list[str]:
    listed = {figure.slug for figure in recorded}
    return [
        *_find_repeated_slugs(published),
        *[reason for figure in recorded for reason in _find_figure_difference(figure, published)],
        *_find_unlisted_figures(published, listed),
    ]


def _find_repeated_slugs(published: dict[str, list[JsonScalar]]) -> list[str]:
    return [
        f"the rebuilt run published more than one figure under slug '{slug}'; a rebuild "
        "matches figures by slug, so each slug must name one figure"
        for slug, values in sorted(published.items())
        if len(values) > 1
    ]


def _find_figure_difference(
    figure: RecipeFigure, published: dict[str, list[JsonScalar]]
) -> list[str]:
    if figure.slug not in published:
        return [
            f"the recipe lists figure '{figure.slug}' = {figure.value!r}, which the rebuilt "
            "run did not publish"
        ]
    values = published[figure.slug]
    if len(values) > 1 or _match_exactly(values[0], figure.value):
        return []
    return [
        f"figure '{figure.slug}': the rebuilt run published {values[0]!r}; the recipe "
        f"recorded {figure.value!r}"
    ]


def _find_unlisted_figures(published: dict[str, list[JsonScalar]], listed: set[str]) -> list[str]:
    return [
        f"the rebuilt run published figure '{slug}' = {', '.join(map(repr, values))}, which "
        "the recipe does not list"
        for slug, values in sorted(published.items())
        if slug not in listed
    ]


def _match_exactly(rebuilt: JsonScalar, recorded: JsonScalar) -> bool:
    # Type too: 12 and 12.0, or 1 and True, compare equal in Python yet print differently.
    return type(rebuilt) is type(recorded) and rebuilt == recorded
