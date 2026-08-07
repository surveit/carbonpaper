"""Workflow-test seam: run a workflow — any subset of its stages, over any slice of
its real source — as a REAL run: same `<project_dir>/runs/<id>/` dir, manifest, and
routes as a production run, but marked `RunManifest.is_test_run` and scoped
read-only. It reaches the shared engine through app.runtime.executor (run_subset),
never app.runtime.runner."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import pandas as pd

from app.core.errors import NoWorkflowTestSourceError, NoWorkflowTestVersionError, SubsetRunError
from app.models import Stage, StageType, Workflow
from app.models.run_manifest import OverwrittenFrame
from app.runtime.context import RunContext, RunIdentity
from app.runtime.executor import run_subset, topological_sort
from app.models.run_parameters import RunParameters
from app.models.stages.input_data import InputDataStage
from app.runtime.stages.execution import narrow_stage
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
    """Run the resolved version, as a real run (marked `is_test_run`) under
    `<project_dir>/runs/<run_id>/`. Returns `{ok, run_id, version_id, stages_run, error}`.

    The same run app.services.run.start_run produces, differing on exactly six
    axes — the reason this is its own seam rather than a flag on that one:

    1. VERSION: any stored version, published or not (_resolve_workflow_test_version).
    2. SOURCE: every input stage reads its own bound file, cut to `limit` rows from
       `offset` by the runtime's per-stage window (`limit=None` means the whole file).
       Only a source `stage_ids` cuts off is handed a frame instead — recorded as an
       OVERWRITTEN stage, never as a silent hole.
    3. SCOPE: `stage_ids` names the stages to execute; None runs every stage
       (_frontier_stages). A source the scope leaves out is read here and handed in
       (_read_source_slices), over the same window it would have read itself.
    4. EXECUTION: synchronous; start_run launches a background daemon thread.
    5. REVIEW QUEUE: auto-approves in memory (queue_auto_approve) instead of halting.
    6. STAGE CACHE: read-only (RunContext.for_workflow_test_run) instead of read+write.

    Collapsing these into start_run would mean six flags with two valid
    combinations, so they stay two functions; only version resolution is shared
    vocabulary (cf. app.services.run.resolve_version, which gates on published)."""
    project_dir = resolve_project_dir(project)
    version = _resolve_workflow_test_version(project_dir, version_id)
    stages = load_version_stages(project_dir, version)
    executing = topological_sort(_stages_to_execute(stages, stage_ids))
    # Read the source(s) before building the Workflow, so a sourceless workflow
    # fails on the missing source rather than on downstream graph validation.
    slices = _read_source_slices(stages, executing, limit=limit, offset=offset)
    injected = {sid: sliced.frame for sid, sliced in slices.items()}
    workflow = Workflow(stages=stages)

    run_id = _mint_run_id()
    run_dir = resolve_run_dir(project, run_id)

    executed_ids = [stage.id for stage in executing]
    limits, offsets = _source_row_windows(executing, limit, offset)
    ok, error = _run_frontier(
        workflow, injected, executed_ids, run_dir, repo_root(),
        project=project_dir.name, run_id=run_id, workflow_version=version,
        limits=limits, offsets=offsets,
        overwritten={sid: sliced.overwritten_by for sid, sliced in slices.items()})

    return {
        "ok": ok,
        "run_id": run_id,
        "version_id": version,
        "stages_run": executed_ids,
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
    limits: dict[str, int],
    offsets: dict[str, int],
    overwritten: dict[str, OverwrittenFrame],
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
            overwritten=overwritten,
            run_dir=run_dir, repo_root=repo_root,
            params=RunParameters(limits=limits, offsets=offsets,
                                 queue_auto_approve=True, is_test_run=True),
            project=project, workflow_version=workflow_version,
            identity=RunIdentity(project=project, run_id=run_id))
    except SubsetRunError as exc:
        return False, str(exc)
    return True, None


def _stages_to_execute(stages: list[Stage], stage_ids: list[str] | None) -> list[Stage]:
    """The stages to execute: exactly `stage_ids`, or every non-input stage when it is None."""
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
    """Every stage, sources included: a workflow test LOADS its own data."""
    # An input stage reads its bound file here exactly as a production run's does, cut
    # to the same `limit`/`offset` window by the runtime. Handing it a frame instead
    # would overwrite the one stage whose whole job is to read that file, and cost the
    # run the record of having read it.
    return list(stages)


def _source_row_windows(
    executing: list[Stage], limit: int | None, offset: int,
) -> tuple[dict[str, int], dict[str, int]]:
    """The same `limit`/`offset` window, for each source stage that executes instead."""
    sources = [
        stage.id for stage in executing if stage.type == StageType.input_data.value
    ]
    limits = {} if limit is None else {sid: limit for sid in sources}
    offsets = {} if offset == 0 else {sid: offset for sid in sources}
    return limits, offsets


class SourceSlice(NamedTuple):
    """One source stage's rows as this run received them, and where they came from."""

    frame: pd.DataFrame
    overwritten_by: OverwrittenFrame


def _read_source_slices(
    stages: list[Stage], executing: list[Stage], *, limit: int | None, offset: int,
) -> dict[str, SourceSlice]:
    """`iloc[offset:offset+limit]` per source stage the scope cut off; usually none of them."""
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
    return {
        source.id: _read_source_slice(source, ctx, limit=limit, offset=offset)
        for source in sources
        if source.id not in executing_ids
    }


def _read_source_slice(
    source: Stage, ctx: RunContext, *, limit: int | None, offset: int
) -> SourceSlice:
    """One source read, with the provenance the run records for it."""
    whole = read_input_data(source, ctx)
    # The file and its hash are the same two facts a production run's preflight
    # records; the window and the rows available say what was taken from it.
    end = None if limit is None else offset + limit
    path = narrow_stage(source, InputDataStage).connector.params.get("path")
    return SourceSlice(
        frame=whole.iloc[offset:end],
        overwritten_by=OverwrittenFrame(
            origin="source_file",
            path=str(path) if path else None,
            sha256=_read_file_sha256(path),
            rows_available=len(whole),
            limit=limit,
            offset=offset,
        ),
    )


def _read_file_sha256(path: object) -> str | None:
    """The bound file's hash, or None where the stage names no path."""
    if not isinstance(path, str) or not path:
        return None  # never a placeholder: a wrong hash is worse than a missing one
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _mint_run_id() -> str:
    """A fresh run id, in the same timestamp format app.runtime.runner mints
    production run ids with, so a workflow test's run sorts and reads
    consistently among a project's other runs."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")
