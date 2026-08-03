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
from app.models.stages.input_data import InputDataStage
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
    """Never creates a version: a run is read-only with respect to versions."""
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
    """Also returns each connector stage's param source: "run" (a binding applied) or "workflow"."""
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
    """Re-validates the whole Connector so a bad param fails at prepare, not mid-run."""
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
    """Aggregates every stage's preflight issues into one error, so a caller fixes them in one pass."""
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
    """Writes an all-pending `running` manifest up front, so a caller can poll while the run executes."""
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
    """Safe to run in a background thread; the manifest is updated on disk as stages complete."""
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
    return run_prepared(
        prepare_run(project_dir, repo_root, version_id,
                    limits=limits, offsets=offsets, bindings=bindings,
                    bust_cache=bust_cache)
    )


def resume_run(project_dir: Path, run_id: str, repo_root: Path) -> dict[str, Any]:
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
