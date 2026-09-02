"""Production run seam: the one service module allowed to drive app.runtime.runner's
production run-lifecycle entry points (enforced by an import-linter contract). Every
other run driver goes through here rather than importing the runner directly."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
from pydantic import ValidationError

from app.core.background import run_in_background
from app.core.errors import RunVersionUnresolvableError
from app.core.frames import read_frame_column_names
from app.models import Workflow, WorkflowStage
from app.models.run_manifest import read_run_bindings
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.runtime.cancellation import discard_cancel
from app.models.records.run_manifest import RunManifest
from app.runtime.manifest import (
    RunEntry as RunEntry,
    list_run_entries as list_run_entries,
    read_run_entry,
    read_run_manifest,
    read_stage_output_frame,
    resolve_output_path,
)
from app.runtime.runner import prepare_run, resume_run, run_prepared
from app.runtime.citations import build_row_trace_url as build_row_trace_url
from app.services.errors import WorkflowLoadError
from app.services.run_manifest_metadata import name_run
from app.services.versioning import load_version, load_version_stages, resolve_version_id
from app.models.records.workflow_version import WorkflowVersion
from app.services.workspace import resolve_run_dir, resolve_runs_dir


def list_every_run_entry() -> list[RunEntry]:
    """Every run the store holds. One outlives its project's working copy, and so does its cost."""
    entries = [read_run_entry(*_split_run_key(key)) for key in RunManifest.list_ids()]
    return sorted(entries, key=lambda entry: (entry.project, entry.area, entry.run_id))


def _split_run_key(key: str) -> tuple[str, str, str]:
    """`RunManifest.compose_id` undone. A run id may hold a slash; the first two segments may not."""
    project_id, area, run_id = key.split("/", 2)
    return project_id, area, run_id


def start_run(
    project_id: str,
    *,
    version_id: str | None = None,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
    name: str = "",
) -> str:
    prep = _prepare(project_id, version_id, bindings, limits, offsets, bust_cache)
    run_id = str(prep["run_id"])
    # Named before execution, so the redirect lands on a named page.
    if name.strip():
        name_run(project_id, run_id, name)
    _run_in_background(run_prepared, prep)
    return run_id


def execute(
    project_id: str,
    *,
    version_id: str | None = None,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    return run_prepared(
        _prepare(project_id, version_id, bindings, limits, offsets, bust_cache)
    )


def _prepare(
    project_id: str,
    version_id: str | None,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None,
    limits: dict[str, int] | None,
    offsets: dict[str, int] | None,
    bust_cache: bool,
) -> dict[str, Any]:
    workflow_version = resolve_version_id(project_id, version_id)
    return prepare_run(
        resolve_runs_dir(project_id),
        project_id,
        Workflow(stages=load_version_stages(project_id, workflow_version)),
        workflow_version,
        limits=limits,
        offsets=offsets,
        bindings=bindings,
        bust_cache=bust_cache,
    )


def resume(project_id: str, run_id: str) -> None:
    workflow_version = read_pinned_version(project_id, run_id)
    discard_cancel(project_id, run_id)
    _run_in_background(
        resume_run,
        resolve_run_dir(project_id, run_id),
        project_id,
        run_id,
        Workflow(stages=load_version_stages(project_id, workflow_version)),
        workflow_version,
    )


def read_pinned_version(project_id: str, run_id: str) -> str:
    workflow_version = read_run_manifest(project_id, run_id).workflow_version
    if not workflow_version:
        raise RunVersionUnresolvableError(
            f"Run '{run_id}' of '{project_id}' records no workflow version in its "
            f"manifest, so the workflow it executed cannot be identified — it "
            f"cannot be resumed."
        )
    return workflow_version


def read_stage_output(project_id: str, run_id: str, stage_id: str) -> pd.DataFrame:
    run_dir = resolve_run_dir(project_id, run_id)
    _validate_run_exists(project_id, run_id)
    return read_stage_output_frame(project_id, run_dir, stage_id)


def read_output_column_counts(project_id: str, manifest: Mapping[str, Any]) -> dict[str, int]:
    run_id = manifest.get("run_id")
    if not run_id:
        return {}
    run_dir = resolve_run_dir(project_id, str(run_id))
    # Off the frames the run wrote, never off what the version's signatures promise:
    # most stage types do not trim their output frame to the schema they declared, so
    # the frame may carry columns the promise never named. A frame that cannot be read
    # has no count here at all.
    counted = {
        str(record["stage_id"]): _count_output_columns(run_dir, record.get("output_path"))
        for record in manifest.get("stage_records", [])
    }
    return {stage_id: count for stage_id, count in counted.items() if count is not None}


def read_run_status(project_id: str, run_id: str) -> dict[str, Any]:
    return read_run_manifest(project_id, run_id).to_dict()


def read_run_status_without_tracebacks(project_id: str, run_id: str) -> dict[str, Any]:
    """What an agent may read: the run page keeps the traceback, behind its own disclosure."""
    return read_run_manifest(project_id, run_id).to_dict_without_tracebacks()


def _count_output_columns(run_dir: Path, output_path: str | None) -> int | None:
    try:
        path = resolve_output_path(run_dir, output_path)
        if path is None or not path.exists():
            return None
        return len(read_frame_column_names(path))
    except (OSError, ValueError):
        # An unreadable frame (a path escaping the run, a truncated file) leaves the
        # width unknown. StageOutputMissing is a ValueError, so it lands here too.
        return None


def _validate_run_exists(project_id: str, run_id: str) -> None:
    """Raises RunNotFoundError unless the project recorded a run of this id."""
    read_run_manifest(project_id, run_id)


def resolve_version(project_id: str, version_id: str | None) -> str:
    return resolve_version_id(project_id, version_id)


def load_run_version(project_id: str, manifest: dict[str, Any]) -> WorkflowVersion:
    version_id = manifest.get("workflow_version")
    if not version_id:
        raise RunVersionUnresolvableError(
            f"This run of '{project_id}' records no workflow version in its "
            "manifest, so the workflow it executed cannot be identified."
        )
    try:
        return load_version(project_id, str(version_id))
    except (FileNotFoundError, WorkflowLoadError) as exc:
        raise RunVersionUnresolvableError(
            f"This run of '{project_id}' pinned workflow version "
            f"'{version_id}', which could not be read: {exc}"
        ) from exc


def load_run_workflow(project_id: str, manifest: dict[str, Any]) -> Workflow:
    pinned = _build_pinned_workflow(project_id, manifest)
    # The snapshot alone is not what ran — the manifest carries the binding as a
    # separate delta, and resume_run replays it. So must every reader, or a panel
    # shows a file the run never opened.
    try:
        bound, _ = pinned.apply_run_bindings(read_run_bindings(manifest))
    except ValueError as exc:
        # The bindings and the pinned version disagree — the run cannot be
        # reconstituted, which is what this error already means to every caller.
        raise RunVersionUnresolvableError(
            f"run {manifest.get('run_id')} records bindings that do not fit its "
            f"pinned version {manifest.get('workflow_version')}: {exc}"
        ) from exc
    return bound


def _build_pinned_workflow(project_id: str, manifest: dict[str, Any]) -> Workflow:
    try:
        return Workflow(stages=load_run_version(project_id, manifest).stages)
    except ValidationError as exc:
        raise RunVersionUnresolvableError(
            f"run {manifest.get('run_id')} pinned workflow version "
            f"{manifest.get('workflow_version')}, whose stages no longer form a "
            f"workflow: {exc}"
        ) from exc


@dataclass(frozen=True)
class RunStageDef:
    """None both for an unreadable version and for no such stage; `error` tells them apart."""

    workflow_stage: WorkflowStage | None
    error: str | None


def load_pinned_stage_def(
    project_id: str, manifest: dict[str, Any], stage_id: str
) -> RunStageDef:
    try:
        workflow = load_run_workflow(project_id, manifest)
    except RunVersionUnresolvableError as exc:
        return RunStageDef(workflow_stage=None, error=str(exc))
    return RunStageDef(
        workflow_stage=workflow.index_workflow_stages_by_id().get(stage_id),
        error=None,
    )


def _run_in_background(target: Callable[..., object], *args: object) -> None:
    """The seam a test replaces to run a triggered run inline; a crash prints its traceback."""
    run_in_background(partial(target, *args))
