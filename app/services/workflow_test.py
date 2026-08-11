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
from app.models import Stage, StageType, Workflow
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset, topological_sort
from app.models.run_parameters import RunParameters
from app.runtime.stages.input_data import read_input_data
from app.services.versioning import list_versions, load_version, load_version_stages
from app.services.workspace import repo_root, resolve_project_dir, resolve_run_dir


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
    executing = topological_sort(_stages_to_execute(stages, stage_ids))
    # Read the source(s) before building the Workflow, so a sourceless workflow
    # fails on the missing source rather than on downstream graph validation.
    injected = _read_source_slices(stages, executing, limit=limit, offset=offset)
    workflow = Workflow(stages=stages)

    run_id = mint_timestamp_id()
    run_dir = resolve_run_dir(project, run_id)

    executed_ids = [stage.id for stage in executing]
    limits, offsets = _source_row_windows(executing, limit, offset)
    ok, error = _run_frontier(
        workflow, injected, executed_ids, run_dir, repo_root(),
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
    repo_root: Path,
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
            run_dir=run_dir, repo_root=repo_root,
            params=RunParameters(limits=limits, offsets=offsets,
                                 queue_auto_approve=True, is_test_run=True),
            project=project, workflow_version=workflow_version,
            identity=RunIdentity(project=project, run_id=run_id))
    except SubsetRunError as exc:
        return False, str(exc)
    return True, None


def _stages_to_execute(stages: list[Stage], stage_ids: list[str] | None) -> list[Stage]:
    if stage_ids is None:
        return _frontier_stages(stages)
    by_id = {stage.id: stage for stage in stages}
    unknown = [sid for sid in stage_ids if sid not in by_id]
    if unknown:
        raise ValueError(
            f"version has no stage(s) {unknown} — its stages: {sorted(by_id)}"
        )
    return [by_id[sid] for sid in stage_ids]


def _frontier_stages(stages: list[Stage]) -> list[Stage]:
    return [stage for stage in stages if stage.type != StageType.input_data.value]


def _source_row_windows(
    executing: list[Stage], limit: int | None, offset: int,
) -> tuple[dict[str, int], dict[str, int]]:
    sources = [
        stage.id for stage in executing if stage.type == StageType.input_data.value
    ]
    limits = {} if limit is None else {sid: limit for sid in sources}
    offsets = {} if offset == 0 else {sid: offset for sid in sources}
    return limits, offsets


def _read_source_slices(
    stages: list[Stage], executing: list[Stage], *, limit: int | None, offset: int,
) -> dict[str, pd.DataFrame]:
    sources = [stage for stage in stages if stage.type == StageType.input_data.value]
    if not sources:
        raise NoWorkflowTestSourceError(
            "workflow has no input_data stage to read a workflow-test slice from")
    # Ephemeral context: read_input_data reads only the stage's connector params
    # (an absolute bound path), never repo_root/run_dir or project scope — so this
    # source read carries the real repo_root and no run_dir (None, the read
    # precedes any run-dir creation) rather than a fabricated cwd sentinel.
    ctx = RunContext.for_stages_outside_a_run(repo_root(), None)
    executing_ids = {stage.id for stage in executing}
    end = None if limit is None else offset + limit
    return {
        source.id: read_input_data(source, ctx).iloc[offset:end]
        for source in sources
        if source.id not in executing_ids
    }
