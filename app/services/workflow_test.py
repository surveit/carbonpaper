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

from app.core.errors import (
    NoWorkflowTestSourceError,
    NoWorkflowTestVersionError,
    SubsetRunError,
    WorkflowTestStageScopeError,
    WorkflowTestTargetConflictError,
)
from app.models import Stage, StageType, Workflow
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset, topological_sort
from app.runtime.stages.input_data import read_input_data
from app.services.loader import load_workflow, stage_to_spec_dict
from app.services.versioning import (
    create_version_from_stages,
    list_versions,
    load_version,
    load_version_stages,
)
from app.services.workspace import repo_root, resolve_project_dir


def run_workflow_test(
    project: str,
    *,
    version_id: str | None = None,
    use_working_copy: bool = False,
    only_stages: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Run a workflow's frontier over a slice of its bound source, as a real run
    (marked `is_test_run`) under `<project_dir>/runs/<run_id>/`. Returns
    `{ok, run_id, version_id, stages_run, error}`.

    WHICH workflow is an explicit choice with no overlap: `use_working_copy` runs
    the project's `compiled/` stages — what the authoring tools edit, unsaved —
    freezing them as a version first (_mint_working_copy_version) so the run pins
    an immutable snapshot of exactly what it executed, like every other run;
    otherwise a STORED version runs, `version_id` naming it or None resolving to
    the newest (_resolve_workflow_test_version). Asking for both raises
    WorkflowTestTargetConflictError rather than one silently winning.

    `only_stages` SCOPES which stages execute; omitted, the whole frontier runs.
    A scoped input_data stage EXECUTES, reading its whole bound file — the way to
    observe an input column's complete vocabulary. See _resolve_stage_scope.

    The same run app.services.run.start_run produces, differing on exactly five
    axes — the reason this is its own seam rather than a flag on that one:

    1. WORKFLOW: any stored version, published or not, or the working copy.
    2. SOURCE: a `limit`/`offset` slice, injected (get_source_data_with_limit_and_offset)
       rather than read whole through the input_data stage — which is why the
       frontier excludes input_data (_frontier_stages). run_subset still persists
       that slice as the input stage's own output, so every stage of the graph is
       readable back off the run.
    3. EXECUTION: synchronous; start_run launches a background daemon thread.
    4. REVIEW QUEUE: auto-approves in memory (queue_auto_approve) instead of halting.
    5. STAGE CACHE: read-only (RunContext.for_workflow_test_run) instead of read+write.

    Collapsing these into start_run would mean five flags with two valid
    combinations, so they stay two functions; only version resolution is shared
    vocabulary (cf. app.services.run.resolve_version, which gates on published)."""
    project_dir = resolve_project_dir(project)
    stages, stored = _resolve_workflow_test_stages(project_dir, version_id, use_working_copy)
    scope = _resolve_stage_scope(stages, only_stages)
    # Read the source(s) before building the Workflow, so a sourceless workflow
    # fails on the missing source rather than on downstream graph validation.
    injected = get_source_data_with_limit_and_offset(
        stages, limit=limit, offset=offset, executed_ids={stage.id for stage in scope})
    workflow = Workflow(stages=stages)
    # Last, so a refused scope or an unreadable source writes no snapshot.
    version = stored if stored is not None else _mint_working_copy_version(project_dir, stages)

    run_id = _mint_run_id()
    run_dir = project_dir / "runs" / run_id

    stage_ids = [stage.id for stage in topological_sort(scope)]
    ok, error = _run_scoped_stages(
        workflow, injected, stage_ids, run_dir, repo_root(),
        project=project_dir.name, run_id=run_id, workflow_version=version)

    return {
        "ok": ok,
        "run_id": run_id,
        "version_id": version,
        "stages_run": stage_ids,
        "error": error,
    }


def _resolve_workflow_test_stages(
    project_dir: Path, version_id: str | None, use_working_copy: bool
) -> tuple[list[Stage], str | None]:
    """The stages to run, and the STORED version they came from — None for the working copy."""
    if not use_working_copy:
        version = _resolve_workflow_test_version(project_dir, version_id)
        return load_version_stages(project_dir, version), version
    if version_id is not None:
        raise WorkflowTestTargetConflictError(
            f"workflow test of '{project_dir.name}' asked for BOTH version "
            f"'{version_id}' and the working copy — name one: drop version_id to "
            "run the working copy, or drop use_working_copy to run the version")
    return load_workflow(project_dir), None


def _mint_working_copy_version(project_dir: Path, stages: list[Stage]) -> str:
    """Freeze the working copy so the run pins an immutable snapshot of what it ran."""
    # Born unpublished like any other version — only a human publishes — and marked
    # minted_for_workflow_test so an auto-minted snapshot is told apart from an
    # authored one by a field rather than by the wording of `message`.
    version = create_version_from_stages(
        project_dir, [stage_to_spec_dict(stage) for stage in stages],
        message="Working copy, frozen to run a workflow test",
        reviewer="workflow_test",
        minted_for_workflow_test=True)
    return version.version_id


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


def _run_scoped_stages(
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
    """A SubsetRunError (a stage errored, or the run halted) becomes (False, its message)."""
    try:
        run_subset(
            workflow, injected_outputs=injected, stage_ids=stage_ids,
            run_dir=run_dir, repo_root=repo_root, queue_auto_approve=True,
            project=project, workflow_version=workflow_version,
            identity=RunIdentity(project=project, run_id=run_id), is_test_run=True)
    except SubsetRunError as exc:
        return False, str(exc)
    return True, None


def _resolve_stage_scope(stages: list[Stage], only_stages: list[str] | None) -> list[Stage]:
    """The stages this workflow test executes: `only_stages`, else the whole frontier."""
    if only_stages is None:
        return _frontier_stages(stages)
    by_id = {stage.id: stage for stage in stages}
    unknown = [stage_id for stage_id in only_stages if stage_id not in by_id]
    if unknown:
        raise WorkflowTestStageScopeError(
            f"workflow test scoped to stage(s) this workflow does not have: {unknown} — "
            f"its stages are {sorted(by_id)}")
    scope = [by_id[stage_id] for stage_id in only_stages]
    _validate_scope_covers_its_upstreams(scope, set(only_stages), by_id)
    return scope


def _validate_scope_covers_its_upstreams(
    scope: list[Stage], scoped_ids: set[str], by_id: dict[str, Stage]
) -> None:
    """Raise unless every producer a scoped stage reads is itself scoped or injectable."""
    # Only an input_data stage can be supplied from outside the scope, because only
    # its output can be read off a bound file rather than computed. Any other absent
    # producer would leave a scoped stage running on nothing — refused up front, so a
    # partial run never comes back looking like a complete one.
    missing = sorted({
        input_id
        for stage in scope
        for input_id in stage.input_ids
        if input_id not in scoped_ids
        and (input_id not in by_id
             or by_id[input_id].type != StageType.input_data.value)
    })
    if missing:
        raise WorkflowTestStageScopeError(
            f"workflow test scope leaves stage(s) {missing} unproduced, and only an "
            f"input_data stage can be supplied from outside the scope — add them to "
            f"only_stages: {sorted({*scoped_ids, *missing})}")


def _frontier_stages(stages: list[Stage]) -> list[Stage]:
    """The stages a workflow test executes: every stage except the source
    (input_data — its output is injected, not computed, though run_subset still
    writes it out and records it). Publish stages run; their artifacts land
    run-scoped under the run dir, like any production run's."""
    return [stage for stage in stages if stage.type != StageType.input_data.value]


def get_source_data_with_limit_and_offset(
    stages: list[Stage], *, limit: int, offset: int, executed_ids: set[str],
) -> dict[str, pd.DataFrame]:
    """Read each input_data stage's bound file and take `df.iloc[offset:offset+limit]`,
    keyed by stage id — SKIPPING a source in `executed_ids`, which the run executes
    itself and so reads whole, unsliced. Raises NoWorkflowTestSourceError if the
    workflow declares no input_data stage at all."""
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
        if source.id not in executed_ids
    }


def _mint_run_id() -> str:
    """A fresh run id, in the same timestamp format app.runtime.runner mints
    production run ids with, so a workflow test's run sorts and reads
    consistently among a project's other runs."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
