"""Production run-lifecycle entry points, executing the version-pinned `Workflow` a
caller hands them - the only functions that create a production run record. The engine
they call and the non-production subset executor live in `app.runtime.executor`;
contracts enforce that split, and that this module never imports `app.services`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.lib as pa_lib

from app.core.errors import MissingInputBindingError
from app.core.timestamp_ids import mint_timestamp_id
from app.core.frames import read_frame_table
from app.models import StageType, Workflow, WorkflowStage
from app.models.run_parameters import RunParameters
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.core.run_status import StageStatus, is_run_still_going

from .branch_analysis import load_run_branches
from .context import RunContext
from .executor import _execute_stages, topological_sort
from .manifest import (
    RunManifest,
    read_run_manifest,
    create_run_manifest,
    resolve_output_path,
    write_manifest,
)
from .errors import MissingLineage, NotALoadStage
from .stages import PREFLIGHTS


def validate_stages_ready(
    stages: list[WorkflowStage], param_sources: dict[StageId, str]
) -> dict[str, dict[str, Any]]:
    issues: list[str] = []
    records: dict[str, dict[str, Any]] = {}
    for workflow_stage in stages:
        preflight = PREFLIGHTS.get(StageType(workflow_stage.stage.type))
        if preflight is None:
            continue
        stage_issues, record = preflight(workflow_stage)
        issues.extend(stage_issues)
        if record is not None:
            records[workflow_stage.id] = {**record, "source": param_sources[workflow_stage.id]}
    if issues:
        raise MissingInputBindingError("; ".join(issues))
    return records


def prepare_run(
    runs_dir: Path,
    project_id: str,
    workflow: Workflow,
    workflow_version: str,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    """`limits`/`offsets` window each named stage's INPUT rows, not its output; offset applies first."""
    bound, param_sources = workflow.apply_run_bindings(bindings)
    workflow_stages = bound.list_workflow_stages()
    input_records = validate_stages_ready(workflow_stages, param_sources)
    ordered = topological_sort(workflow_stages)

    limits = dict(limits or {})
    offsets = dict(offsets or {})
    stage_ids = {s.id for s in ordered}
    for flag, mapping in (("--limit", limits), ("--offset", offsets)):
        unknown = set(mapping) - stage_ids
        if unknown:
            raise ValueError(
                f"{flag} targets unknown stage id(s): {sorted(unknown)}; "
                f"stages are {[s.id for s in ordered]}"
            )

    run_id = mint_timestamp_id()
    run_dir = runs_dir / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    # This run's logical identity for cancellation's checkpoints (see
    # app.runtime.cancellation) — read by _execute_stages, never by name of
    # anything on disk. run_dir above stays I/O-only.
    ctx = RunContext.for_workflow_run(
        run_dir, project_id, run_id,
        RunParameters(
            limits=dict(limits or {}),
            offsets=dict(offsets or {}),
            bust_cache=bust_cache,
            run_bindings={sid: dict(p) for sid, p in (bindings or {}).items()},
        ),
    )
    # The manifest's shape and persistence belong to the executor — it mints the
    # initial record here and rewrites the same file as stages run. What prepare
    # adds is the stage-owned preflight provenance; everything the caller asked for
    # is already on the ctx this run will execute against.
    manifest = create_run_manifest(
        ordered,
        ctx,
        run_id=run_id,
        project_id=project_id,
        workflow_version=workflow_version,
        input_bindings=input_records,
    )
    write_manifest(manifest)
    return {"run_id": run_id, "run_dir": run_dir, "ctx": ctx,
            "ordered": ordered, "manifest": manifest}


def run_prepared(prep: dict[str, Any]) -> dict[str, Any]:
    manifest = _execute_stages(prep["ordered"], prep["ctx"], prep["manifest"],
                               prep["run_dir"], outputs_so_far={})
    _keep_branch_analysis(manifest, prep["ordered"], prep["run_dir"])
    return manifest.to_dict()


def _keep_branch_analysis(
    manifest: RunManifest, ordered: list[WorkflowStage], run_dir: Path,
) -> None:
    """Worked out here, off the request path: a page would spend a run's own time on it."""
    if manifest.workflow_version is None or is_run_still_going(manifest.status):
        return
    sized = [(record.stage_id, record.output_row_count)
             for record in manifest.stage_records if record.output_path]
    rows = dict(sized)
    try:
        load_run_branches(run_dir, {stage.id: stage for stage in ordered if stage.id in rows},
                          [stage_id for stage_id, _ in sized], rows,
                          manifest.workflow_version)
    # Left unkept, the reader works it out and says why on the page, as it did before.
    except (MissingLineage, NotALoadStage, OSError, pa_lib.ArrowException):
        return


def execute_run(
    runs_dir: Path,
    project_id: str,
    workflow: Workflow,
    workflow_version: str,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[StageId, TypeUnsafeUserStageConfigOverride] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    return run_prepared(
        prepare_run(runs_dir, project_id, workflow, workflow_version,
                    limits=limits, offsets=offsets, bindings=bindings,
                    bust_cache=bust_cache)
    )


def resume_run(
    run_dir: Path,
    project_id: str,
    run_id: str,
    workflow: Workflow,
    workflow_version: str,
) -> dict[str, Any]:
    manifest = read_run_manifest(project_id, run_id)

    if manifest.workflow_version != workflow_version:
        raise ValueError(
            f"Run {run_id} of '{project_id}' is pinned to workflow version "
            f"{manifest.workflow_version!r}, but stages for {workflow_version!r} "
            f"were supplied; a resume must execute the version the run started on."
        )
    # Replay this run's bindings (recorded verbatim by prepare_run) onto the
    # freshly-reloaded stages. Without this, a stage that had not yet executed
    # when the run halted would resume on its workflow-authored params (or fail
    # if it authors none) while the manifest still claims `source: "run"` — a
    # false provenance record.
    bound, _ = workflow.apply_run_bindings(manifest.parameters.run_bindings)
    ordered = topological_sort(bound.list_workflow_stages())

    # Reload outputs from disk for stages that completed successfully.
    outputs_so_far: dict[str, pa.Table] = {}
    for record in manifest.stage_records:
        if record.status not in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS):
            continue
        try:
            path = resolve_output_path(run_dir, record.output_path)
            if path is None or not path.exists():
                continue
            outputs_so_far[record.stage_id] = read_frame_table(path)
        except (pa_lib.ArrowException, pd.errors.ParserError, OSError, ValueError):
            # A prior output file that's missing/corrupt/unreadable is
            # treated as not-yet-produced; the stage simply re-runs.
            pass

    # This run's logical identity for cancellation's checkpoints — see the
    # matching comment in prepare_run. Stamped here too so a resumed run is
    # cancellable, not just a fresh one.
    # Replayed wholesale: the recorded parameters ARE what a resume must execute
    # under — the same row windows, the same refusal to read the cache — rather
    # than quietly reusing what the halted run skipped.
    build_context = (RunContext.for_workflow_test_run if manifest.parameters.is_test_run
                     else RunContext.for_workflow_run)
    # Auto-approve is legal only against the read-only cache a test run ran under.
    ctx = build_context(run_dir, project_id, run_id, manifest.parameters)
    # The run's telemetry (human_review_queue_stats/dropped_columns) already lives on the
    # loaded manifest, not the context; a resumed run keeps accumulating onto
    # that same manifest via the executor's per-stage merge.

    manifest.resumed_at = datetime.now().isoformat(timespec="seconds")
    # Drop the halt marker the halted run left behind: the run is no longer
    # halted — it is resuming — so a mid-run flush() (which persists status
    # `running`) must not carry `halted_at`, or the run page would show the
    # "halted for review" banner and queue links while the stage re-runs. The
    # loop re-adds `halted_at` if a stage halts again; otherwise it stays gone.
    manifest.clear_halt()
    settled = _execute_stages(ordered, ctx, manifest, run_dir, outputs_so_far)
    _keep_branch_analysis(settled, ordered, run_dir)
    return settled.to_dict()
