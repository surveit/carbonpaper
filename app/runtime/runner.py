"""
Workflow runner.

Topologically orders stages, runs each one through its type-specific handler,
validates the input + output schema, and persists per-stage outputs as parquet
(plus a manifest.json summarising the run).

Run output layout:
    examples/<project>/runs/<run_id>/
        manifest.json
        outputs/<stage_id>.parquet
        artifacts/<...>           # for publish stages
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

import pandas as pd
import pyarrow.lib as pa_lib
from pydantic import ValidationError as PydanticValidationError

from app.core.errors import MissingInputBindingError, NoVersionToRunError, SubsetRunError
from app.core.models import Connector, Stage, StageType, Workflow
from app.services.loader import WorkflowLoadError
from app.services import versioning

from .cancellation import RunCancelled, clear, is_cancelled
from .stages import HANDLERS, PREFLIGHTS, HaltForReview
from .validation import Issue, validate_dataframe


def topological_sort(stages: list[Stage]) -> list[Stage]:
    by_id = {s.id: s for s in stages}
    visited: set[str] = set()
    order: list[Stage] = []

    def visit(sid: str, path: list[str]) -> None:
        if sid in visited:
            return
        if sid in path:
            raise ValueError(f"Cycle detected: {' → '.join(path + [sid])}")
        for iid in by_id[sid].input_ids:
            if iid in by_id:
                visit(iid, path + [sid])
        visited.add(sid)
        order.append(by_id[sid])

    for sid in by_id:
        visit(sid, [])
    return order


def _duplicate_row_groups(df: pd.DataFrame) -> list[list[int]]:
    """Groups of 0-based row positions whose FULL row content is identical.
    Identity is a content hash over every column's string-rendered value —
    the declared primary_key plays no part (it is optional and may
    legitimately duplicate)."""
    if df is None or len(df) == 0:
        return []
    groups: dict[str, list[int]] = {}
    for pos, cells in enumerate(df.itertuples(index=False, name=None)):
        # repr() (not str()) so cells of different types with the same face
        # value ("1" vs 1) stay distinct, and NaN/None/lists all render.
        rendered = "\x1f".join(repr(c) for c in cells)
        digest = hashlib.sha1(rendered.encode("utf-8")).hexdigest()
        groups.setdefault(digest, []).append(pos)
    return [positions for positions in groups.values() if len(positions) > 1]


def _reject_duplicate_input_rows(df: pd.DataFrame, input_id: str, stage_id: str) -> None:
    """Fail the stage if an input dataframe contains exact duplicate
    full-content rows. Duplicates at a stage boundary are ambiguous intent —
    either an upstream bug, or sampling smuggled in implicitly. If N draws
    per row are intended, the author adds an explicit row_id/draw_id column
    upstream, making the rows distinct."""
    dupes = _duplicate_row_groups(df)
    if not dupes:
        return
    shown = "; ".join(f"rows {group}" for group in dupes[:5])
    more = f" (+{len(dupes) - 5} more group(s))" if len(dupes) > 5 else ""
    raise ValueError(
        f"Input '{input_id}' to stage '{stage_id}' contains exact duplicate "
        f"rows: {shown}{more} (0-based row numbers). Duplicates at a stage "
        "boundary are ambiguous intent — an upstream bug, or sampling smuggled "
        "in implicitly. If N draws per row are intended, add an explicit "
        "row_id/draw_id column upstream so the rows are distinct."
    )


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

    ctx: dict[str, Any] = {
        "repo_root": repo_root,
        "run_dir": run_dir,
        "project_dir": project_dir,
        # This run's logical identity for cancellation's checkpoints (see
        # app.runtime.cancellation) — read by _execute_stages, never by name
        # of anything on disk. run_dir above stays I/O-only.
        "project": project_dir.name,
        "run_id": run_id,
        "queue_stats": {},
        "dropped_columns": {},
        "limits": limits,
        "offsets": offsets,
    }
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "project": project_dir.name,
        "workflow_version": workflow_version,
        "limit_overrides": limits,
        "offset_overrides": offsets,
        # The run's bindings verbatim (generic bookkeeping a resume replays),
        # alongside the stage-owned preflight provenance records.
        "run_bindings": {sid: dict(params) for sid, params in (bindings or {}).items()},
        "input_bindings": input_records,
        "status": "running",
        "stages": [
            {"stage_id": s.id, "type": s.type, "name": s.name,
             "status": "pending", "input_validation": [], "output_validation": None,
             "elapsed_ms": 0, "rows": 0, "error": None,
             "started_at": None, "finished_at": None}
            for s in ordered
        ],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )
    return {"run_id": run_id, "run_dir": run_dir, "ctx": ctx,
            "ordered": ordered, "manifest": manifest}


def run_prepared(prep: dict[str, Any]) -> dict[str, Any]:
    """Execute a run previously set up by prepare_run(). Suitable for running in
    a background thread (the manifest is updated on disk as stages complete)."""
    return _execute_stages(prep["ordered"], prep["ctx"], prep["manifest"],
                           prep["run_dir"], outputs_so_far={})


def execute_run(
    project_dir: Path,
    repo_root: Path,
    version_id: str | None = None,
    limits: dict[str, int] | None = None,
    offsets: dict[str, int] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run the workflow once (synchronous). Returns the manifest dict. `version_id`
    pins the run to a workflow version (None -> newest published; none published ->
    NoVersionToRunError); see prepare_run / resolve_version_id.
    `limits`/`offsets` are per-run row slicing overrides; `bindings` is the
    per-run connector-param override; see prepare_run."""
    return run_prepared(
        prepare_run(project_dir, repo_root, version_id,
                    limits=limits, offsets=offsets, bindings=bindings)
    )


def run_subset(
    workflow: Workflow,
    *,
    injected_outputs: dict[str, pd.DataFrame],
    stage_ids: list[str],
    run_dir: Path,
    repo_root: Path,
) -> dict[str, pd.DataFrame]:
    """Run only `stage_ids` of `workflow`, with `injected_outputs` seeded as the
    outputs of stages OUTSIDE the subset (their upstream is cut off — the output is
    given, not computed). Returns the outputs of every executed stage.

    Any input of a subset stage that names a stage outside the subset must appear in
    `injected_outputs`, or `_execute_stages` fails on it. Raises SubsetRunError if an
    executed stage errors or the run halts for review, so a caller gets a clean output
    set or a loud failure — never a half-populated dict."""
    by_id = {stage.id: stage for stage in workflow.stages}
    missing = [sid for sid in stage_ids if sid not in by_id]
    if missing:
        raise SubsetRunError(f"subset names stage(s) not in the workflow: {missing}")
    ordered = topological_sort([by_id[sid] for sid in stage_ids])
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    outputs: dict[str, pd.DataFrame] = dict(injected_outputs)
    manifest = _execute_stages(
        ordered, _subset_ctx(repo_root, run_dir), _subset_manifest(run_dir, ordered),
        run_dir, outputs)
    _raise_if_run_failed(manifest)
    return outputs


def _subset_ctx(repo_root: Path, run_dir: Path) -> dict[str, Any]:
    # No project_dir: a subset run is keyed on the Workflow + run_dir, not a project
    # tree. A handler that needs project-relative state (only human_review_queue does,
    # and it halts a subset run anyway) fails loudly on the missing key rather than
    # reading a fabricated wrong directory.
    return {"repo_root": repo_root, "run_dir": run_dir,
            "queue_stats": {}, "limits": {}, "offsets": {}}


def _subset_manifest(run_dir: Path, ordered: list[Stage]) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "status": "running",
        "stages": [{"stage_id": s.id, "type": s.type, "name": s.name,
                    "status": "pending", "input_validation": [], "output_validation": None,
                    "elapsed_ms": 0, "rows": 0, "error": None,
                    "started_at": None, "finished_at": None}
                   for s in ordered],
    }


def _raise_if_run_failed(manifest: dict[str, Any]) -> None:
    """Turn a non-clean manifest into a SubsetRunError naming the cause. Reads the
    same status/stage records `_execute_stages` writes — the manifest is the run's
    result of record, so failure detection lives with it, not in each caller."""
    status = manifest.get("status")
    if status in ("ok", "warnings"):
        return
    if status == "awaiting_review":
        raise SubsetRunError(
            f"run halted for human review at {manifest.get('halted_at')!r}")
    for stage in manifest.get("stages", []):
        if stage.get("status") == "error":
            error = stage.get("error") or {}
            raise SubsetRunError(
                f"stage {stage['stage_id']!r} errored: {error.get('message', 'unknown error')}")
    raise SubsetRunError(f"run did not complete (status {status!r})")


def _summarize_row_errors(row_errors: list[dict[str, Any]]) -> str:
    """One-line summary of per-row generation failures for the stage's error
    record — the per-row detail lives in output_validation issues."""
    head = "; ".join(f"row {e['row']}: {e['message']}" for e in row_errors[:3])
    more = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
    return f"{len(row_errors)} row(s) failed generation: {head}{more}"


def _read_run_identity(ctx: dict[str, Any]) -> tuple[str, str] | None:
    """This run's logical (project, run_id) identity, read off ctx — the keys
    prepare_run/resume_run stamp for cancellation's checkpoints. None when
    either is absent: a subset/eval run's ctx (built by _subset_ctx) carries
    neither key, so those runs are simply not cancellable."""
    project, run_id = ctx.get("project"), ctx.get("run_id")
    if not isinstance(project, str) or not isinstance(run_id, str):
        return None
    return project, run_id


def _is_run_cancelled(ctx: dict[str, Any]) -> bool:
    """True if this run has a cancel requested (see _read_run_identity for
    when a run is cancellable at all)."""
    identity = _read_run_identity(ctx)
    return identity is not None and is_cancelled(*identity)


def _execute_stages(
    ordered: list[Stage],
    ctx: dict[str, Any],
    manifest: dict[str, Any],
    run_dir: Path,
    outputs_so_far: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Execute ordered stages, honoring HaltForReview and RunCancelled.

    Stages whose ids are already in `outputs_so_far` are skipped (their
    output was computed in a prior partial run and loaded from disk by
    the resume path). On HaltForReview the loop stops; remaining stages
    are appended as `pending` records and manifest status is
    `awaiting_review`.

    On a cancel request (polled via ctx's `project`/`run_id` — see
    app.runtime.cancellation) the loop stops the same way and manifest status
    is `cancelled`: between stages, before the next one starts (it stays
    `pending`); or mid-stage, via RunCancelled unwinding out of
    handler.execute (that stage's own record is marked `cancelled`).
    Stages already completed keep their `ok` record and on-disk output."""
    halted: HaltForReview | None = None
    halt_at_index: int = -1
    cancelled = False
    cancel_at_index: int = -1

    # Carry over any existing records (from a previously halted manifest
    # we're resuming). Build an index for upsert behavior.
    records_by_id: dict[str, dict[str, Any]] = {
        r["stage_id"]: r for r in manifest.get("stages", [])
    }

    def _pending_stub(s: Stage) -> dict[str, Any]:
        return {
            "stage_id": s.id, "type": s.type, "name": s.name,
            "status": "pending", "input_validation": [], "output_validation": None,
            "elapsed_ms": 0, "rows": 0, "error": None,
            "started_at": None, "finished_at": None,
        }

    def flush(status: str = "running") -> None:
        """Write the manifest mid-run so the run page can show live progress
        (stages light up as they start/finish) instead of the whole pipeline
        running silently and updating only at the very end."""
        m = dict(manifest)
        m["stages"] = [records_by_id.get(s.id) or _pending_stub(s) for s in ordered]
        m["status"] = status
        m["queue_stats"] = ctx.get("queue_stats", {})
        m["dropped_columns"] = ctx.get("dropped_columns", {})
        m["updated_at"] = datetime.now().isoformat(timespec="seconds")
        try:
            (run_dir / "manifest.json").write_text(
                json.dumps(m, indent=2, default=str), encoding="utf-8"
            )
        except OSError:
            pass

    flush("running")  # initial: all stages pending

    for idx, stage in enumerate(ordered):
        # Between-stage cancel checkpoint: before this stage starts (even
        # before checking whether it's a resume-skip), stop if a cancel was
        # requested. No exception, no record written here — the stage simply
        # never starts, so it stays (or becomes) `pending` below.
        if _is_run_cancelled(ctx):
            cancelled = True
            cancel_at_index = idx
            break

        sid = stage.id
        stype = stage.type

        # Skip stages already produced (resume path).
        if sid in outputs_so_far and records_by_id.get(sid, {}).get("status") in ("ok", "validation_warnings"):
            continue

        record: dict[str, Any] = {
            "stage_id": sid,
            "type": stype,
            "name": stage.name,
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "status": "running",
            "input_validation": [],
            "output_validation": None,
            "elapsed_ms": 0,
            "rows": 0,
            "error": None,
        }
        t0 = time.perf_counter()
        records_by_id[sid] = record
        flush("running")  # show this stage as running

        try:
            inputs_for_stage: dict[str, pd.DataFrame] = {}
            for ref in stage.inputs:
                if ref.id not in outputs_so_far:
                    raise RuntimeError(f"Upstream stage '{ref.id}' has no output yet")
                df = outputs_so_far[ref.id]
                _reject_duplicate_input_rows(df, ref.id, sid)
                inputs_for_stage[ref.id] = df
                if ref.table_schema is not None:
                    rep = validate_dataframe(
                        df, ref.table_schema, stage_id=sid, phase=f"input:{ref.id}",
                    )
                    record["input_validation"].append(rep.to_dict())

            handler = HANDLERS.get(stype)
            if handler is None:
                raise ValueError(f"No handler for stage type '{stype}'")

            try:
                output = handler.execute(stage, inputs_for_stage, ctx)
            except HaltForReview as halt:
                record["status"] = "awaiting_review"
                record["rows"] = halt.pending_count
                record["queue_path"] = str(halt.queue_path.relative_to(run_dir))
                record["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                records_by_id[sid] = record
                halted = halt
                halt_at_index = idx
                break
            except RunCancelled:
                # Mid-stage cancel: the row driver unwound out of
                # handler.execute (see execution.py::_run_row_mapper). This
                # stage made no output — it is marked cancelled, not ok.
                record["status"] = "cancelled"
                record["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                records_by_id[sid] = record
                cancelled = True
                cancel_at_index = idx
                break

            if output is None:
                output = pd.DataFrame()

            # Generic row slicing, in the handler's emitted order. Offset
            # (per-run only, from --offset stage=M) drops the first M rows;
            # then the cap keeps the first N. A per-run cap (--limit stage=N)
            # wins over the stage's static `limit:`. Used to throttle /
            # page the expensive LLM fan-out.
            offset = (ctx.get("offsets") or {}).get(sid)
            if isinstance(offset, int) and offset > 0 and len(output) > 0:
                record.setdefault("notes", []).append(
                    f"offset={offset}: dropped first {min(offset, len(output))} of {len(output)} row(s)"
                )
                output = output.iloc[offset:].reset_index(drop=True).copy()
            limit = (ctx.get("limits") or {}).get(sid, stage.limit)
            if isinstance(limit, int) and limit >= 0 and len(output) > limit:
                record.setdefault("notes", []).append(
                    f"limit={limit}: truncated from {len(output)} to {limit} row(s)"
                )
                output = output.head(limit).copy()

            out_rep = validate_dataframe(
                output, stage.output_schema, stage_id=sid, phase="output",
            )
            row_errors = (ctx.get("row_errors") or {}).get(sid, [])
            if row_errors:
                out_rep.issues[0:0] = [
                    Issue("error", None,
                          f"row {row_error['row']}: generation failed: {row_error['message']}")
                    for row_error in row_errors
                ]
            record["output_validation"] = out_rep.to_dict()

            output_path = run_dir / "outputs" / f"{sid}.parquet"
            try:
                output.to_parquet(output_path, index=False)
            except (pa_lib.ArrowException, ValueError, TypeError) as exc:
                # A column whose dtype/shape parquet can't represent (mixed-type
                # object columns, nested Python values) falls back to CSV, which
                # stringifies them, rather than losing the stage output; the
                # fallback is recorded, never silent. A disk/OS error is NOT
                # caught: it would fail identically for CSV, so it propagates to
                # the per-stage handler below and lands in the manifest.
                output_path = run_dir / "outputs" / f"{sid}.csv"
                output.to_csv(output_path, index=False)
                record.setdefault("notes", []).append(
                    f"Wrote CSV instead of parquet: {exc}"
                )

            outputs_so_far[sid] = output
            if row_errors:
                record["status"] = "error"
                record["error"] = {
                    "type": "RowGenerationError",
                    "message": _summarize_row_errors(row_errors),
                    "traceback": None,
                }
            else:
                record["status"] = "ok" if out_rep.ok and all(
                    v["ok"] for v in record["input_validation"]
                ) else "validation_warnings"
            record["rows"] = int(len(output))
            record["output_path"] = str(output_path.relative_to(run_dir))

        except Exception as exc:  # noqa: BLE001 — the runner's contract is
            # to record ANY stage failure (a handler can raise ValueError,
            # RuntimeError, a pandas/pyarrow error, etc.) in the manifest and
            # continue/halt rather than crash the whole run.
            record["status"] = "error"
            record["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            }
            outputs_so_far[sid] = pd.DataFrame()
        finally:
            # awaiting_review / cancelled records finalize themselves (above)
            # so their own custom fields aren't clobbered by a second,
            # slightly-later timestamp here.
            if record["status"] not in ("awaiting_review", "cancelled"):
                record["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
                record["finished_at"] = datetime.now().isoformat(timespec="seconds")
                records_by_id[sid] = record
            flush("running")  # persist this stage's result for the live page

    # If halted, mark remaining stages as pending so the workflow can render
    # them greyed out.
    if halted is not None:
        for stage in ordered[halt_at_index + 1:]:
            sid = stage.id
            records_by_id[sid] = {
                "stage_id": sid,
                "type": stage.type,
                "name": stage.name,
                "status": "pending",
                "input_validation": [],
                "output_validation": None,
                "elapsed_ms": 0,
                "rows": 0,
                "error": None,
                "started_at": None,
                "finished_at": None,
            }

    # If cancelled, mark not-yet-run stages pending from cancel_at_index on.
    # The between-stage path's own stage never got a record (not-yet-started
    # stays pending); the mid-stage path's already has one (status
    # "cancelled", set above) and is left alone by the `not in` guard.
    if cancelled:
        for stage in ordered[cancel_at_index:]:
            if stage.id not in records_by_id:
                records_by_id[stage.id] = _pending_stub(stage)

    # Emit stages in topological order so the manifest reads top-to-bottom.
    manifest["stages"] = [records_by_id[s.id] for s in ordered if s.id in records_by_id]
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["queue_stats"] = ctx.get("queue_stats", {})
    manifest["dropped_columns"] = ctx.get("dropped_columns", {})

    if halted is not None:
        manifest["status"] = "awaiting_review"
        manifest["halted_at"] = halted.stage_id
    elif cancelled:
        # Never run the normal ok/errors/warnings computation for a cancelled
        # run — a run stopped by request is neither a clean completion nor a
        # failure.
        manifest["status"] = "cancelled"
        manifest["cancelled_at"] = ordered[cancel_at_index].id
        manifest.pop("halted_at", None)
    else:
        manifest["status"] = (
            "ok" if all(s["status"] == "ok" for s in manifest["stages"])
            else "errors" if any(s["status"] == "error" for s in manifest["stages"])
            else "warnings"
        )
        manifest.pop("halted_at", None)

    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )

    # The registry entry must not outlive the run: clear() fires on the normal
    # completion path and on the halt/cancel paths (both `break` out of the loop
    # and fall through to this single return). A hard crash mid-write would skip
    # it, leaking one completed run's (project, run_id) key — harmless, since run
    # ids are per-second timestamps that already collide on run_dir.
    identity = _read_run_identity(ctx)
    if identity is not None:
        clear(*identity)
    return manifest


def resume_run(project_dir: Path, run_id: str, repo_root: Path) -> dict[str, Any]:
    """Resume a previously halted run. Loads existing outputs from disk,
    re-runs the halted queue stage (decisions now exist), continues
    downstream, updates the same manifest in place."""
    run_dir = project_dir / "runs" / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest at {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Stay pinned to the SAME workflow snapshot the run started on. We read the
    # version off the existing manifest and reload the version's stages — never
    # the live working copy — so a resume can't silently execute a different workflow
    # than the halted run did. A run that carries no workflow_version is a pre-
    # versioning (legacy) run we cannot safely resume under the version model;
    # fail loudly rather than guessing which snapshot it meant.
    workflow_version = manifest.get("workflow_version")
    if not workflow_version:
        raise ValueError(
            f"Run {run_id} of '{project_dir.name}' has no 'workflow_version' in "
            f"its manifest ({manifest_path}); cannot resume a versioned run "
            f"without its pinned workflow version."
        )
    stages = versioning.load_version_stages(project_dir, workflow_version)
    # Replay this run's bindings (recorded verbatim by prepare_run) onto the
    # freshly-reloaded stages. Without this, a stage that had not yet executed
    # when the run halted would resume on its workflow-authored params (or fail
    # if it authors none) while the manifest still claims `source: "run"` — a
    # false provenance record. Manifests from before this feature carry no
    # `run_bindings` key; `.get(..., {})` keeps those resuming exactly as
    # before.
    stages, _ = apply_run_bindings(stages, manifest.get("run_bindings", {}))
    ordered = topological_sort(stages)

    # Reload outputs from disk for stages that completed successfully.
    outputs_so_far: dict[str, pd.DataFrame] = {}
    for record in manifest.get("stages", []):
        if record.get("status") not in ("ok", "validation_warnings"):
            continue
        op = record.get("output_path")
        if not op:
            continue
        path = run_dir / op
        if not path.exists():
            continue
        try:
            if path.suffix == ".parquet":
                outputs_so_far[record["stage_id"]] = pd.read_parquet(path)
            else:
                outputs_so_far[record["stage_id"]] = pd.read_csv(path)
        except (pa_lib.ArrowException, pd.errors.ParserError, OSError, ValueError):
            # A prior output file that's missing/corrupt/unreadable is
            # treated as not-yet-produced; the stage simply re-runs.
            pass

    ctx: dict[str, Any] = {
        "repo_root": repo_root,
        "run_dir": run_dir,
        "project_dir": project_dir,
        # This run's logical identity for cancellation's checkpoints — see the
        # matching comment in prepare_run. Stamped here too so a resumed run
        # is cancellable, not just a fresh one.
        "project": project_dir.name,
        "run_id": run_id,
        "queue_stats": manifest.get("queue_stats", {}),
        "dropped_columns": manifest.get("dropped_columns", {}),
        # Re-apply the run's per-stage row slicing so stages that resume after
        # a halt honor the same limits/offsets the run started with.
        "limits": manifest.get("limit_overrides") or {},
        "offsets": manifest.get("offset_overrides") or {},
    }

    manifest["resumed_at"] = datetime.now().isoformat(timespec="seconds")
    return _execute_stages(ordered, ctx, manifest, run_dir, outputs_so_far)


# CLI entrypoint for ad-hoc runs
def main() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m app.runtime.runner <project_dir> "
              "[--limit <stage_id>=<N> ...] [--offset <stage_id>=<M> ...]")
        return 1
    project_dir = Path(args[0]).resolve()
    limits: dict[str, int] = {}
    offsets: dict[str, int] = {}
    i = 1
    while i < len(args):
        if args[i] in ("--limit", "--offset") and i + 1 < len(args) and "=" in args[i + 1]:
            stage_id, _, n = args[i + 1].partition("=")
            (limits if args[i] == "--limit" else offsets)[stage_id] = int(n)
            i += 2
        else:
            print(f"Unknown argument: {args[i]}")
            return 1
    repo_root = Path(__file__).resolve().parents[2]
    try:
        manifest = execute_run(project_dir, repo_root,
                               limits=limits or None, offsets=offsets or None)
    except (NoVersionToRunError, WorkflowLoadError) as exc:
        print(exc)
        return 1
    print(json.dumps(
        {"run_id": manifest["run_id"], "workflow_version": manifest["workflow_version"],
         "status": manifest["status"],
         "stages": [(s["stage_id"], s["status"], s["rows"]) for s in manifest["stages"]]},
        indent=2,
    ))
    return 0 if manifest["status"] == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
