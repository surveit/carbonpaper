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
    """Run the resolved version's frontier over a slice of its bound source, as a
    real run (marked `is_test_run`) under `<project_dir>/runs/<run_id>/`. Returns
    `{ok, run_id, version_id, stages_run, error}`.

    The same run app.services.run.start_run produces, differing on exactly five
    axes — the reason this is its own seam rather than a flag on that one:

    1. VERSION: any stored version, published or not (_resolve_workflow_test_version).
    2. SOURCE: a `limit`/`offset` slice, injected (get_source_data_with_limit_and_offset)
       rather than read whole through the input_data stage — which is why the
       frontier excludes input_data (_frontier_stages).
    3. EXECUTION: synchronous; start_run launches a background daemon thread.
    4. REVIEW QUEUE: auto-approves in memory (queue_auto_approve) instead of halting.
    5. STAGE CACHE: read-only (RunContext.for_workflow_test_run) instead of read+write.

    Collapsing these into start_run would mean five flags with two valid
    combinations, so they stay two functions; only version resolution is shared
    vocabulary (cf. app.services.run.resolve_version, which gates on published)."""
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
    """The stored immutable version a workflow test runs — any version, published
    or not, unlike a production run (which pins a published version). A workflow
    test is the tool the author uses to evaluate a candidate BEFORE deciding to
    publish, so gating it on publication would be circular. None resolves to the
    newest stored version; raises NoWorkflowTestVersionError, naming the project,
    when None is given and no version is stored."""
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
    """Execute the frontier subset: normal return -> (True, None); a SubsetRunError
    (a stage errored) -> (False, its message). run_subset owns the manifest under
    `run_dir`, records it `is_test_run=True`, and grants project scope
    (`identity` + a read-only stage cache — see RunContext.for_stages_outside_a_run)
    so a publish stage's `trace_links` resolves; a mid-frontier
    human_review_queue auto-approves in memory (queue_auto_approve=True) rather
    than halting."""
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
    """The stages a workflow test executes: every stage except the source
    (input_data — its output is injected, not computed). Publish stages run; their
    artifacts land run-scoped under the run dir, like any production run's."""
    return [stage for stage in stages if stage.type != StageType.input_data.value]


def get_source_data_with_limit_and_offset(
    stages: list[Stage], *, limit: int, offset: int,
) -> dict[str, pd.DataFrame]:
    """Read each input_data stage's bound file and take `df.iloc[offset:offset+limit]`,
    keyed by stage id. Raises NoWorkflowTestSourceError if the workflow declares no
    input_data stage."""
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
    """A fresh run id, in the same timestamp format app.runtime.runner mints
    production run ids with, so a workflow test's run sorts and reads
    consistently among a project's other runs."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
