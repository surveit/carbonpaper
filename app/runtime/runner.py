"""Production workflow runner - the run-lifecycle entry points.

These are the only functions that create a production run record; the reusable
engine they call and the non-production subset executor both live in
`app.runtime.executor`, and an import-linter contract enforces that split.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pyarrow.lib as pa_lib
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import MissingInputBindingError, NoVersionToRunError
from app.core.frames import PARQUET_SUFFIX
from app.core.store_config import configure_default_stores
from app.models import Connector, Stage, StageType
from app.core.run_status import RunStatus, StageStatus
from app.services.errors import WorkflowLoadError
from app.services import versioning

from .context import RunContext
from .executor import _execute_stages, topological_sort
from .manifest import (
    create_run_manifest,
    load_manifest_model,
    write_manifest,
)
from .stages import PREFLIGHTS


def resolve_version_id(project_dir: Path, version_id: str | None) -> str:
    """Resolve the workflow version a run will be pinned to. Every run MUST target a
    real, PUBLISHED version — we never blank it, never fabricate one, never
    silently read the working copy, and never CREATE one as a run side effect.
    A run is read-only with respect to versions.

    - If `version_id` is given, it must name an existing, published version; we
      fail loudly otherwise rather than redirecting to some other snapshot or
      silently running an unreviewed draft.
    - If `version_id` is None, pin to the newest PUBLISHED version (an
      unpublished version more recent than it is skipped).
    - If no version exists, or none is published, raise NoVersionToRunError. A
      run will not immortalise the working copy as a version (that is what let
      an invalid working copy poison "the latest" and fail every subsequent
      run), and a run will not treat an unreviewed draft as runnable.
    """
    if version_id is not None:
        # Validate the requested version exists (load_version fails loudly if
        # its version.json is missing) — a caller asking for a specific id
        # must not be silently redirected to some other snapshot.
        version = versioning.load_version(project_dir, version_id)
        if not version.published:
            raise NoVersionToRunError(
                f"Version '{version_id}' of '{project_dir.name}' is not published. "
                f"A run pins a published version — publish it first."
            )
        return version_id

    for version in versioning.list_versions(project_dir):  # newest-first
        if version.published:
            return version.version_id

    raise NoVersionToRunError(
        f"No published version to run for '{project_dir.name}'. A run "
        f"targets a published version and never creates one — save a version "
        f"and publish it first."
    )


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
    connector_ids = {s.id for s in stages if s.connector is not None}
    given = dict(bindings or {})
    unbindable = sorted(set(given) - connector_ids)
    if unbindable:
        raise ValueError(
            f"bindings target stage id(s) with no connector to bind: {unbindable}; "
            f"bindable stages are {sorted(connector_ids)}")

    rebound = [
        _merge_connector_params(stage, given[stage.id]) if stage.id in given else stage
        for stage in stages
    ]
    param_sources = {
        sid: "run" if sid in given else "workflow" for sid in connector_ids
    }
    return rebound, param_sources


def _merge_connector_params(stage: Stage, binding: Mapping[str, Any]) -> Stage:
    """A copy of `stage` with `binding` merged over its connector params,
    re-validated as a whole Connector so a bad param fails at prepare, not
    mid-run."""
    if not isinstance(binding, Mapping):
        raise ValueError(
            f"binding for `{stage.id}` must be a dict of connector params, "
            f"got {type(binding).__name__}: {binding!r}")
    assert stage.connector is not None  # caller filters to connector-carrying stages
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
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    """Create the run dir + id and write an initial `running` manifest (all
    stages pending) so a caller can redirect to the run page immediately and
    poll it while execution proceeds in the background. Returns a dict with the
    run_id, run_dir, ctx, ordered stages and the manifest.

    The run is PINNED to a workflow version: stages are loaded from the version's
    immutable snapshot (versioning.load_version_stages), never from the live
    `compiled/` working copy, so working-copy edits can never affect this run.
    `version_id` resolution is documented on resolve_version_id (None -> the
    newest PUBLISHED version; a project with no published version raises
    NoVersionToRunError); the resolved id is recorded in the manifest as
    `workflow_version`.

    `limits` is a per-RUN row-cap override: {stage_id: N} truncates that
    stage's output to its first N rows for this run only, overriding any
    static `limit:` in the stage spec. `offsets` ({stage_id: M}) drops the
    first M rows BEFORE the cap is applied — together they page through a
    deterministic ordering (offset 5 + limit 3 = rows 6-8). Both are recorded
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

    Raises NoVersionToRunError (no version exists, or none is published) or
    WorkflowLoadError (from the version snapshot's strict load) before the run
    dir is created, so a run with no published version — or an invalid
    workflow — never leaves a run behind.
    The same holds for a binding/preflight failure: it is raised before the
    run dir is created."""
    workflow_version = resolve_version_id(project_dir, version_id)
    stages = versioning.load_version_stages(project_dir, workflow_version)
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

    runs_dir = project_dir / "runs"
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S")
    run_dir = runs_dir / run_id
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    # This run's logical identity for cancellation's checkpoints (see
    # app.runtime.cancellation) — read by _execute_stages, never by name of
    # anything on disk. run_dir above stays I/O-only.
    ctx = RunContext.for_production_run(
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
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
    bust_cache: bool = False,
) -> dict[str, Any]:
    """Run the workflow once (synchronous). Returns the manifest dict. `version_id`
    pins the run to a workflow version (None -> newest published; none published ->
    NoVersionToRunError); see prepare_run / resolve_version_id.
    `limits`/`offsets` are per-run row slicing overrides; `bindings` is the
    per-run connector-param override; `bust_cache` recomputes everything; see
    prepare_run."""
    return run_prepared(
        prepare_run(project_dir, repo_root, version_id,
                    limits=limits, offsets=offsets, bindings=bindings,
                    bust_cache=bust_cache)
    )


def resume_run(project_dir: Path, run_id: str, repo_root: Path) -> dict[str, Any]:
    """Resume a previously halted run. Loads existing outputs from disk,
    re-runs the halted queue stage (decisions now exist), continues
    downstream, updates the same manifest in place."""
    run_dir = project_dir / "runs" / run_id
    manifest = load_manifest_model(run_dir)

    # Stay pinned to the SAME workflow snapshot the run started on. We read the
    # version off the existing manifest and reload the version's stages — never
    # the live working copy — so a resume can't silently execute a different workflow
    # than the halted run did. A run that carries no workflow_version is a pre-
    # versioning (legacy) run we cannot safely resume under the version model;
    # fail loudly rather than guessing which snapshot it meant.
    workflow_version = manifest.workflow_version
    if not workflow_version:
        raise ValueError(
            f"Run {run_id} of '{project_dir.name}' has no 'workflow_version' in "
            f"its manifest ({run_dir / 'manifest.json'}); cannot resume a versioned "
            f"run without its pinned workflow version."
        )
    stages = versioning.load_version_stages(project_dir, workflow_version)
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
        if not record.output_path:
            continue
        path = run_dir / record.output_path
        if not path.exists():
            continue
        try:
            if path.suffix == PARQUET_SUFFIX:
                outputs_so_far[record.stage_id] = pd.read_parquet(path)
            else:
                outputs_so_far[record.stage_id] = pd.read_csv(path)
        except (pa_lib.ArrowException, pd.errors.ParserError, OSError, ValueError):
            # A prior output file that's missing/corrupt/unreadable is
            # treated as not-yet-produced; the stage simply re-runs.
            pass

    # This run's logical identity for cancellation's checkpoints — see the
    # matching comment in prepare_run. Stamped here too so a resumed run is
    # cancellable, not just a fresh one.
    ctx = RunContext.for_production_run(
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


# CLI entrypoint for ad-hoc runs
def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m app.runtime.runner <project_dir> "
              "[--limit <stage_id>=<N> ...] [--offset <stage_id>=<M> ...] "
              "[--bust-cache]")
        return 1
    project_dir = Path(args[0]).resolve()
    limits: dict[str, int] = {}
    offsets: dict[str, int] = {}
    bust_cache = False
    i = 1
    while i < len(args):
        if args[i] in ("--limit", "--offset") and i + 1 < len(args) and "=" in args[i + 1]:
            stage_id, _, n = args[i + 1].partition("=")
            (limits if args[i] == "--limit" else offsets)[stage_id] = int(n)
            i += 2
        elif args[i] == "--bust-cache":
            bust_cache = True
            i += 1
        else:
            print(f"Unknown argument: {args[i]}")
            return 1
    repo_root = Path(__file__).resolve().parents[2]
    # This process has no server lifespan to wire storage for it, and the run it
    # is about to start reads a version out of the document store and may pin a
    # frame in the frame store. Guarded, so a caller that configured its own
    # stores before invoking main() keeps them.
    configure_default_stores()
    try:
        manifest = execute_run(project_dir, repo_root,
                               limits=limits or None, offsets=offsets or None,
                               bust_cache=bust_cache)
    except (NoVersionToRunError, WorkflowLoadError) as exc:
        print(exc)
        return 1
    print(json.dumps(
        {"run_id": manifest["run_id"], "workflow_version": manifest["workflow_version"],
         "status": manifest["status"],
         "stage_records": [(s["stage_id"], s["status"], s["output_row_count"])
                           for s in manifest["stage_records"]]},
        indent=2,
    ))
    return 0 if manifest["status"] == RunStatus.OK else 1


if __name__ == "__main__":
    sys.exit(main())
