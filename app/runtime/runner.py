"""Production run-lifecycle entry points, executing the version-pinned stages a caller
hands them - the only functions that create a production run record. The engine they
call and the non-production subset executor live in `app.runtime.executor`; contracts
enforce that split, and that this module never imports `app.services`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pyarrow.lib as pa_lib
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import MissingInputBindingError
from app.core.timestamp_ids import mint_timestamp_id
from app.core.frames import read_frame_file
from app.models import Stage, StageType
from app.models.run_manifest import read_run_manifest
from app.models.run_parameters import RunParameters
from app.models.stages.input_data import Connector, InputDataStage
from app.core.run_status import StageStatus

from .context import RunContext
from .executor import _execute_stages, topological_sort
from .manifest import (
    create_run_manifest,
    resolve_output_path,
    write_manifest,
)
from .stages import PREFLIGHTS


def apply_run_bindings(
    stages: list[Stage], bindings: Mapping[str, Mapping[str, Any]] | None
) -> tuple[list[Stage], dict[str, str]]:
    connector_ids = {s.id for s in stages if isinstance(s, InputDataStage)}
    given = dict(bindings or {})
    unbindable = sorted(set(given) - connector_ids)
    if unbindable:
        raise ValueError(
            f"bindings target stage id(s) with no connector to bind: {unbindable}; "
            f"bindable stages are {sorted(connector_ids)}")

    rebound: list[Stage] = [
        _merge_connector_params(stage, given[stage.id])
        # `given`'s keys were just checked to be connector_ids, which is exactly
        # the input_data stages — so the isinstance never rejects a bound stage.
        if isinstance(stage, InputDataStage) and stage.id in given
        else stage
        for stage in stages
    ]
    param_sources = {
        sid: "run" if sid in given else "workflow" for sid in connector_ids
    }
    return rebound, param_sources


def _merge_connector_params(
    stage: InputDataStage, binding: Mapping[str, Any]
) -> InputDataStage:
    if not isinstance(binding, Mapping):
        raise ValueError(
            f"binding for `{stage.id}` must be a dict of connector params, "
            f"got {type(binding).__name__}: {binding!r}")
    try:
        connector = Connector.model_validate({
            **stage.connector.model_dump(),
            "params": {**stage.connector.params, **binding},
        })
    except PydanticValidationError as err:
        raise ValueError(f"binding for `{stage.id}` is invalid: {err}") from err
    return stage.model_copy(update={"connector": connector})


def validate_stages_ready(
    stages: list[Stage], param_sources: dict[str, str]
) -> dict[str, dict[str, Any]]:
    issues: list[str] = []
    records: dict[str, dict[str, Any]] = {}
    for stage in stages:
        preflight = PREFLIGHTS.get(StageType(stage.type))
        if preflight is None:
            continue
        stage_issues, record = preflight(stage)
        issues.extend(stage_issues)
        if record is not None:
            records[stage.id] = {**record, "source": param_sources[stage.id]}
    if issues:
        raise MissingInputBindingError("; ".join(issues))
    return records


def prepare_run(
    project_dir: Path,
    repo_root: Path,
    stages: list[Stage],
    workflow_version: str,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    """`limits`/`offsets` window each named stage's INPUT rows, not its output; offset applies first."""
    stages, param_sources = apply_run_bindings(stages, bindings)
    input_records = validate_stages_ready(stages, param_sources)
    ordered = topological_sort(stages)

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
    run_dir = project_dir / "runs" / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    # This run's logical identity for cancellation's checkpoints (see
    # app.runtime.cancellation) — read by _execute_stages, never by name of
    # anything on disk. run_dir above stays I/O-only.
    ctx = RunContext.for_workflow_run(
        repo_root, run_dir, project_dir.name, run_id,
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
        project=project_dir.name,
        workflow_version=workflow_version,
        input_bindings=input_records,
    )
    write_manifest(run_dir, manifest)
    return {"run_id": run_id, "run_dir": run_dir, "ctx": ctx,
            "ordered": ordered, "manifest": manifest}


def run_prepared(prep: dict[str, Any]) -> dict[str, Any]:
    manifest = _execute_stages(prep["ordered"], prep["ctx"], prep["manifest"],
                               prep["run_dir"], outputs_so_far={})
    return manifest.to_dict()


def execute_run(
    project_dir: Path,
    repo_root: Path,
    stages: list[Stage],
    workflow_version: str,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    return run_prepared(
        prepare_run(project_dir, repo_root, stages, workflow_version,
                    limits=limits, offsets=offsets, bindings=bindings,
                    bust_cache=bust_cache)
    )


def resume_run(
    project_dir: Path,
    run_id: str,
    repo_root: Path,
    stages: list[Stage],
    workflow_version: str,
) -> dict[str, Any]:
    run_dir = project_dir / "runs" / run_id
    manifest = read_run_manifest(run_dir)

    if manifest.workflow_version != workflow_version:
        raise ValueError(
            f"Run {run_id} of '{project_dir.name}' is pinned to workflow version "
            f"{manifest.workflow_version!r}, but stages for {workflow_version!r} "
            f"were supplied; a resume must execute the version the run started on."
        )
    # Replay this run's bindings (recorded verbatim by prepare_run) onto the
    # freshly-reloaded stages. Without this, a stage that had not yet executed
    # when the run halted would resume on its workflow-authored params (or fail
    # if it authors none) while the manifest still claims `source: "run"` — a
    # false provenance record.
    stages, _ = apply_run_bindings(stages, manifest.parameters.run_bindings)
    ordered = topological_sort(stages)

    # Reload outputs from disk for stages that completed successfully.
    outputs_so_far: dict[str, pd.DataFrame] = {}
    for record in manifest.stage_records:
        if record.status not in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS):
            continue
        try:
            path = resolve_output_path(run_dir, record.output_path)
            if path is None or not path.exists():
                continue
            outputs_so_far[record.stage_id] = read_frame_file(path)
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
    ctx = RunContext.for_workflow_run(
        repo_root, run_dir, project_dir.name, run_id, manifest.parameters)
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
    return _execute_stages(ordered, ctx, manifest, run_dir, outputs_so_far).to_dict()
