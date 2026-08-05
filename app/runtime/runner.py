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
from app.core.frames import read_frame_file
from app.models import Connector, Stage, StageType
from app.models.stages.input_data import InputDataStage
from app.core.run_status import StageStatus

from .context import RunContext
from .executor import _execute_stages, topological_sort
from .manifest import (
    create_run_manifest,
    load_manifest_model,
    resolve_output_path,
    write_manifest,
)
from .stages import PREFLIGHTS


def apply_run_bindings(
    stages: list[Stage], bindings: Mapping[str, Mapping[str, Any]] | None
) -> tuple[list[Stage], dict[str, str]]:
    """Apply per-run bindings to just-loaded stages. A binding is a dict of
    connector params, keyed by stage id, merged over that stage's connector
    params for this run only. Bound stages are replaced by re-validated copies
    (the Connector model enforces its own param rules — e.g. a `path` must be
    absolute); the given stages are never mutated, so the version snapshot
    stays immutable.

    This function knows nothing about what any param MEANS — that a connector
    reads a file, needs a path, has a format. Param semantics live in the
    Connector model (validation) and in each stage type's preflight
    (run-readiness — see stages.PREFLIGHTS).

    Returns (stages, param_sources): param_sources maps every
    connector-carrying stage id to where its effective params came from —
    "run" (a binding was applied) or "workflow" (authored params, untouched).

    Fails loudly on a binding keyed to a stage id that does not exist or
    carries no connector, and on a binding value that is not a dict of params."""
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
    """A copy of `stage` with `binding` merged over its connector params,
    re-validated as a whole Connector so a bad param fails at prepare, not
    mid-run."""
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
    """Run each stage type's preflight — the stage-owned readiness check and
    provenance record (stages.PREFLIGHTS) — over the whole workflow, BEFORE the
    run dir is created. Every issue is aggregated into one
    MissingInputBindingError so a caller fixes all unready stages in one pass.
    Returns the provenance records keyed by stage id, each tagged with where
    its params came from ("run" binding or the "workflow" itself)."""
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
    """Create the run dir + id and write an initial `running` manifest (all
    stages pending) so a caller can redirect to the run page immediately and
    poll it while execution proceeds in the background. Returns a dict with the
    run_id, run_dir, ctx, ordered stages and the manifest.

    The run is PINNED to a workflow version: `stages` are that version's frozen
    snapshot, resolved and loaded by the caller
    (app.services.versioning.resolve_version_id + load_version_stages) — never
    the live `compiled/` working copy, so working-copy edits can never affect
    this run. `workflow_version` is the id those stages came from and is
    recorded in the manifest. Because the caller resolves first, a project with
    no published version — or an invalid snapshot — fails there, before this is
    reached, so no run dir is left behind.

    `limits` is a per-RUN row-cap override: {stage_id: N} caps that stage at
    the first N rows of each of its INPUTS — the handler is never given the
    rest — overriding any static `limit:` in the stage spec. `offsets`
    ({stage_id: M}) skips the first M rows BEFORE the cap is applied — together
    they page through a deterministic ordering (offset 5 + limit 3 = upstream
    rows 6-8). An `input_data` stage has no input frames, so its window is taken
    on the frame it loads instead. Both are recorded
    in the manifest (`limit_overrides` / `offset_overrides`) so the slice is
    part of the run's provenance and survives a halt/resume. Unknown stage
    ids fail loudly.

    `bindings` is a per-run connector-param override: {stage_id: params dict}
    merged over that stage's connector params for this run only (see
    apply_run_bindings — the runner attaches no meaning to the params; the
    Connector model validates them and each stage type's preflight decides
    run-readiness). Each preflight's provenance record — for an input stage,
    the absolute path plus a sha256 + byte count streamed now — lands in the
    manifest (`input_bindings`), tagged with the params' source
    (`"run"`/`"workflow"`). A binding naming a stage with no connector fails
    loudly (ValueError); a stage whose preflight finds it unready — no file
    bound, or the bound file absent — fails loudly (MissingInputBindingError,
    aggregating every unready stage).

    `bust_cache` recomputes everything: the run skips every stage-cache READ
    while still recording what it computes, so the cache ends the run
    re-pinned rather than stale. It is recorded in the manifest and re-applied
    on resume, like the row slicing above. For a human_review_queue stage that
    means no prior decision is replayed — every queueable row halts again and
    the humans are re-asked.

    A binding/preflight failure is raised before the run dir is created, so an
    unready workflow never leaves a run behind."""
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

    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = project_dir / "runs" / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    # This run's logical identity for cancellation's checkpoints (see
    # app.runtime.cancellation) — read by _execute_stages, never by name of
    # anything on disk. run_dir above stays I/O-only.
    ctx = RunContext.for_workflow_run(
        repo_root, run_dir, project_dir.name, run_id, limits=limits, offsets=offsets,
        bust_cache=bust_cache,
    )
    # The manifest's shape and persistence belong to the executor — it mints the
    # initial record here and rewrites the same file as stages run. prepare only
    # supplies the run-level metadata: run_bindings is the run's bindings verbatim
    # (generic bookkeeping a resume replays), alongside the stage-owned preflight
    # provenance records in input_bindings.
    manifest = create_run_manifest(
        ordered,
        run_id=run_id,
        project=project_dir.name,
        workflow_version=workflow_version,
        run_bindings={sid: dict(params) for sid, params in (bindings or {}).items()},
        input_bindings=input_records,
        limits=limits,
        offsets=offsets,
        bust_cache=bust_cache,
        is_test_run=False,
    )
    write_manifest(run_dir, manifest)
    return {"run_id": run_id, "run_dir": run_dir, "ctx": ctx,
            "ordered": ordered, "manifest": manifest}


def run_prepared(prep: dict[str, Any]) -> dict[str, Any]:
    """Execute a run previously set up by prepare_run(). Suitable for running in
    a background thread (the manifest is updated on disk as stages complete).
    Returns the final manifest as a plain JSON-native dict — the same shape a
    reader parses off disk."""
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
    """Run the workflow once (synchronous). Returns the manifest dict. `stages` are
    the frozen stages of `workflow_version`, resolved and loaded by the caller
    (app.services.versioning). `limits`/`offsets` cap the rows each named stage
    READS; `bindings` is the per-run connector-param override; `bust_cache`
    recomputes everything; see prepare_run."""
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
    """Resume a previously halted run. Loads existing outputs from disk,
    re-runs the halted queue stage (decisions now exist), continues
    downstream, updates the same manifest in place.

    A resume stays pinned to the SAME workflow snapshot the halted run started
    on: `stages` must be that snapshot's stages, loaded by the caller for the
    version its manifest records (app.services.run.read_pinned_version). The pin
    is re-checked here, so stages for some other version fail loudly instead of
    silently executing a different workflow than the halted run did."""
    run_dir = project_dir / "runs" / run_id
    manifest = load_manifest_model(run_dir)

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
    stages, _ = apply_run_bindings(stages, manifest.run_bindings)
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
    ctx = RunContext.for_workflow_run(
        repo_root, run_dir, project_dir.name, run_id,
        # Re-apply the run's per-stage row slicing so stages that resume after
        # a halt honor the same limits/offsets the run started with.
        limits=manifest.limit_overrides,
        offsets=manifest.offset_overrides,
        # A resume of a recompute-everything run is still one: replaying the
        # recorded flag keeps the resumed stages refusing the same cache reads
        # the halted run refused, rather than quietly reusing what it skipped.
        bust_cache=manifest.bust_cache,
    )
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
