"""Workflow-test seam: run a workflow — any subset of its stages, over any slice of
its real source — as a REAL run: same `<project_dir>/runs/<id>/` dir, manifest, and
routes as a production run, but marked `RunManifest.is_test_run` and scoped
read-only. It reaches the shared engine through app.runtime.executor (run_subset),
never app.runtime.runner."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import NoWorkflowTestSourceError, NoWorkflowTestVersionError, SubsetRunError
from app.core.timestamp_ids import mint_timestamp_id
from app.models import StageType, Workflow, WorkflowStage
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset, topological_sort
from app.models.run_parameters import RunParameters
from app.runtime.stages.input_data import read_input_data
from app.services.versioning import list_versions, load_version, load_version_stages
from app.services.workspace import resolve_project_dir, resolve_run_dir


def run_workflow_test(
    project: str,
    *,
    version_id: str | None = None,
    stage_ids: list[str] | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> dict[str, Any]:
    project_dir = resolve_project_dir(project)
    version = _resolve_workflow_test_version(project_dir, version_id)
    stages = load_version_stages(project_dir, version)
    # Refused before the Workflow is built, so a sourceless workflow fails on the
    # missing source rather than on downstream graph validation.
    _require_a_source([stage.type for stage in stages])
    workflow = Workflow(stages=stages)
    workflow_stage = workflow.list_workflow_stages()
    executing = topological_sort(_stages_to_execute(workflow_stage, stage_ids))
    injected = _read_source_slices(workflow_stage, executing, limit=limit, offset=offset)

    run_id = mint_timestamp_id()
    run_dir = resolve_run_dir(project, run_id)

    executed_ids = [stage.id for stage in executing]
    limits, offsets = _source_row_windows(executing, limit, offset)
    ok, error = _run_frontier(
        workflow, injected, executed_ids, run_dir,
        project=project_dir.name, run_id=run_id, workflow_version=version,
        limits=limits, offsets=offsets)

    return {
        "ok": ok,
        "run_id": run_id,
        "version_id": version,
        "stages_run": executed_ids,
        "error": error,
    }


def _resolve_workflow_test_version(project_dir: Path, version_id: str | None) -> str:
    """Any stored version, published or not — a workflow test evaluates a candidate."""
    if version_id is not None:
        load_version(project_dir, version_id)  # loud FileNotFoundError if missing
        return version_id
    versions = list_versions(project_dir)  # newest-first
    if not versions:
        raise NoWorkflowTestVersionError(
            f"project '{project_dir.name}' has no stored workflow version to workflow-test")
    return versions[0].version_id


def _run_frontier(
    workflow: Workflow,
    injected: dict[str, pd.DataFrame],
    stage_ids: list[str],
    run_dir: Path,
    *,
    project: str,
    run_id: str,
    workflow_version: str,
    limits: dict[str, int],
    offsets: dict[str, int],
) -> tuple[bool, str | None]:
    try:
        run_subset(
            workflow, injected_outputs=injected, stage_ids=stage_ids,
            run_dir=run_dir,
            params=RunParameters(limits=limits, offsets=offsets,
                                 queue_auto_approve=True, is_test_run=True),
            project=project, workflow_version=workflow_version,
            identity=RunIdentity(project=project, run_id=run_id))
    except SubsetRunError as exc:
        return False, str(exc)
    return True, None


def _stages_to_execute(
    stages: list[WorkflowStage], stage_ids: list[str] | None
) -> list[WorkflowStage]:
    if stage_ids is None:
        return _frontier_stages(stages)
    by_id = {stage.id: stage for stage in stages}
    unknown = [sid for sid in stage_ids if sid not in by_id]
    if unknown:
        raise ValueError(
            f"version has no stage(s) {unknown} — its stages: {sorted(by_id)}"
        )
    return [by_id[sid] for sid in stage_ids]


def _frontier_stages(stages: list[WorkflowStage]) -> list[WorkflowStage]:
    return [stage for stage in stages if not _is_source(stage)]


def _is_source(stage: WorkflowStage) -> bool:
    return stage.stage.type == StageType.input_data.value


def _require_a_source(stage_types: list[StageType]) -> None:
    if any(stage_type == StageType.input_data.value for stage_type in stage_types):
        return
    raise NoWorkflowTestSourceError(
        "workflow has no input_data stage to read a workflow-test slice from")


def _source_row_windows(
    executing: list[WorkflowStage], limit: int | None, offset: int,
) -> tuple[dict[str, int], dict[str, int]]:
    sources = [stage.id for stage in executing if _is_source(stage)]
    limits = {} if limit is None else {sid: limit for sid in sources}
    offsets = {} if offset == 0 else {sid: offset for sid in sources}
    return limits, offsets


def _read_source_slices(
    stages: list[WorkflowStage], executing: list[WorkflowStage], *,
    limit: int | None, offset: int,
) -> dict[str, pd.DataFrame]:
    sources = [stage for stage in stages if _is_source(stage)]
    # Ephemeral context: read_input_data reads only the stage's connector params
    # (an absolute bound path), never run_dir or project scope — so this source
    # read carries no run_dir (None, it precedes any run-dir creation) rather
    # than a fabricated cwd sentinel.
    ctx = RunContext.for_stages_outside_a_run(None)
    executing_ids = {stage.id for stage in executing}
    end = None if limit is None else offset + limit
    return {
        source.id: read_input_data(source, ctx).iloc[offset:end]
        for source in sources
        if source.id not in executing_ids
    }
