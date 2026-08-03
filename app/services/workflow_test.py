"""Workflow-test seam: run a workflow over a slice of its real source as a REAL
run — same `<project_dir>/runs/<id>/` dir, manifest, and routes as a production
run, but marked `RunManifest.is_test_run` and scoped read-only. It reaches the
shared engine through app.runtime.executor (run_subset), never
app.runtime.runner."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.errors import NoWorkflowTestSourceError, NoWorkflowTestVersionError, SubsetRunError
from app.models import Stage, StageType, Workflow
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset, topological_sort
from app.runtime.stages.input_data import read_input_data
from app.services.versioning import list_versions, load_version, load_version_stages
from app.services.workspace import repo_root, resolve_project_dir


def run_workflow_test(
    project: str,
    *,
    version_id: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Its own seam, not a flag on run.start_run: five axes differ, with two valid combinations."""
    project_dir = resolve_project_dir(project)
    version = _resolve_workflow_test_version(project_dir, version_id)
    stages = load_version_stages(project_dir, version)
    # Read the source(s) before building the Workflow, so a sourceless workflow
    # fails on the missing source rather than on downstream graph validation.
    injected = get_source_data_with_limit_and_offset(stages, limit=limit, offset=offset)
    workflow = Workflow(stages=stages)
    frontier = topological_sort(_frontier_stages(stages))

    run_id = _mint_run_id()
    run_dir = project_dir / "runs" / run_id

    stage_ids = [stage.id for stage in frontier]
    ok, error = _run_frontier(
        workflow, injected, stage_ids, run_dir, repo_root(),
        project=project_dir.name, run_id=run_id, workflow_version=version)

    return {
        "ok": ok,
        "run_id": run_id,
        "version_id": version,
        "stages_run": stage_ids,
        "error": error,
    }


def _resolve_workflow_test_version(project_dir: Path, version_id: str | None) -> str:
    """Any version, published or not: a workflow test evaluates a candidate BEFORE publishing it."""
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
) -> tuple[bool, str | None]:
    """A SubsetRunError (a stage errored) becomes (False, its message); a normal run (True, None)."""
    try:
        run_subset(
            workflow, injected_outputs=injected, stage_ids=stage_ids,
            run_dir=run_dir, repo_root=repo_root, queue_auto_approve=True,
            project=project, workflow_version=workflow_version,
            identity=RunIdentity(project=project, run_id=run_id), is_test_run=True)
    except SubsetRunError as exc:
        return False, str(exc)
    return True, None


def _frontier_stages(stages: list[Stage]) -> list[Stage]:
    """Every stage but input_data — its output is injected, not computed. Publish stages DO run."""
    return [stage for stage in stages if stage.type != StageType.input_data.value]


def get_source_data_with_limit_and_offset(
    stages: list[Stage], *, limit: int, offset: int,
) -> dict[str, pd.DataFrame]:
    """Raises NoWorkflowTestSourceError if the workflow declares no input_data stage."""
    sources = [stage for stage in stages if stage.type == StageType.input_data.value]
    if not sources:
        raise NoWorkflowTestSourceError(
            "workflow has no input_data stage to read a workflow-test slice from")
    # Ephemeral context: read_input_data reads only the stage's connector params
    # (an absolute bound path), never repo_root/run_dir or project scope — so this
    # source read carries the real repo_root and no run_dir (None, the read
    # precedes any run-dir creation) rather than a fabricated cwd sentinel.
    ctx = RunContext.for_stages_outside_a_run(repo_root(), None)
    return {
        source.id: read_input_data(source, ctx).iloc[offset:offset + limit]
        for source in sources
    }


def _mint_run_id() -> str:
    """Same timestamp format app.runtime.runner mints production run ids with, so they sort alike."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
