"""
Shared stage-execution engine.

Holds the reusable machinery that runs a set of ordered stages through their
type-specific handlers — validating each stage's input + output schema, honoring
`HaltForReview`/`RunCancelled`, and writing per-stage outputs plus a live
manifest. Two callers sit on top of it:

- `app/runtime/runner.py` — the PRODUCTION run lifecycle (`prepare_run` /
  `execute_run` / `run_prepared` / `resume_run`): creates the `runs/<id>/` dir,
  writes the production run manifest, and pins a published workflow version.
- `run_subset` (here) — the NON-PRODUCTION subset executor used by evals (and
  any preview): runs only a chosen subset of a `Workflow`'s stages, with the
  outputs of stages outside the subset injected rather than computed.

This module never creates a production run record; it only executes stages it is
handed. Keeping it separate from `runner.py` lets an import-linter contract stop
a non-production caller (evals) from ever reaching the production run entry
points — see `pyproject.toml`.
"""

from __future__ import annotations

import enum
import hashlib
import json
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, NotRequired, TypedDict

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.errors import SubsetRunError
from app.models import Stage, StageType, Workflow
from app.core.run_status import RunStatus, StageStatus

from .cancellation import consume_cancel
from .context import RowError, RunContext, RunIdentity
from .errors import RunCancelled
from .stages import HANDLERS, HaltForReview, StageHandler
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
    by_id = workflow.index_stages_by_id()
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


def _subset_ctx(repo_root: Path, run_dir: Path) -> RunContext:
    # No identity/stage_cache: a subset run is keyed on the Workflow + run_dir, not a
    # project tree, and has no cross-run cache access. A handler that needs project
    # scope (only human_review_queue does, and it halts a subset run anyway) fails
    # loudly rather than reading a fabricated wrong directory.
    return RunContext(
        repo_root=repo_root, run_dir=run_dir, identity=None, stage_cache=None,
        limits={}, offsets={},
    )


def _subset_manifest(run_dir: Path, ordered: list[Stage]) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "status": RunStatus.RUNNING,
        "stages": [_build_pending_stage_record(s) for s in ordered],
    }


def _raise_if_run_failed(manifest: dict[str, Any]) -> None:
    """Turn a non-clean manifest into a SubsetRunError naming the cause. Reads the
    same status/stage records `_execute_stages` writes — the manifest is the run's
    result of record, so failure detection lives with it, not in each caller."""
    status = manifest.get("status")
    if status in (RunStatus.OK, RunStatus.WARNINGS):
        return
    if status == RunStatus.AWAITING_REVIEW:
        halted_at = ", ".join(manifest.get("halted_at") or [])
        raise SubsetRunError(f"run halted for human review at {halted_at}")
    for stage in manifest.get("stages", []):
        if stage.get("status") == StageStatus.ERROR:
            error = stage.get("error") or {}
            raise SubsetRunError(
                f"stage {stage['stage_id']!r} errored: {error.get('message', 'unknown error')}")
    raise SubsetRunError(f"run did not complete (status {status!r})")


def _execute_stages(
    ordered: list[Stage],
    ctx: RunContext,
    manifest: dict[str, Any],
    run_dir: Path,
    outputs_so_far: dict[str, pd.DataFrame],
) -> dict[str, Any]:
    """Execute ordered stages, honoring HaltForReview and RunCancelled.

    Stages whose ids are already in `outputs_so_far` are skipped (their
    output was computed in a prior partial run and loaded from disk by
    the resume path).

    A stage's `ok` status asserts every upstream stage succeeded, so error and
    halt are fork-blocking, not loop-ending. An errored stage and a
    HaltForReview stage both go into a `blocked` set; a stage whose any
    input-producer is blocked is skipped (`pending`, no output written) and
    joins the set, so the block propagates to the whole transitive downstream
    while independent forks run to completion. The run status is `errors` if
    any stage errored, else `awaiting_review` if any halted, else the usual
    ok/warnings; `halted_at` lists every halted stage.

    On a cancel request (polled via `ctx.identity` — see
    app.runtime.cancellation) the loop stops dead and manifest status is
    `cancelled`: between stages, before the next one starts (it stays
    `pending`); or mid-stage, via RunCancelled unwinding out of
    handler.execute (that stage's own record is marked `cancelled`).
    Stages already completed keep their `ok` record and on-disk output."""
    halted_stage_ids: list[str] = []
    blocked: set[str] = set()
    cancelled = False
    cancel_at_index: int = -1

    # Carry over any existing records (from a previously halted manifest
    # we're resuming). Build an index for upsert behavior.
    records_by_id: dict[str, StageRecord] = {
        r["stage_id"]: r for r in manifest.get("stages", [])
    }
    _flush_manifest(manifest, records_by_id, ordered, ctx, run_dir, RunStatus.RUNNING)

    for idx, stage in enumerate(ordered):
        # Between-stage cancel checkpoint: before this stage starts (even
        # before checking whether it's a resume-skip), consume a pending cancel
        # message and, if there was one, stop. No exception, no record written
        # here — the stage simply never starts, so it stays `pending` below.
        if _consume_cancel(ctx):
            cancelled = True
            cancel_at_index = idx
            break

        sid = stage.id

        # A stage whose any input-producer errored, halted, or is itself
        # blocked cannot run on real inputs. It stays pending, joins the
        # blocked set so its own downstream follows, and drops any stale output
        # so a resume cannot reuse it. Checked before the resume-skip so a
        # newly-blocked upstream overrides a prior `ok` output on disk.
        if _find_blocking_upstream(stage, blocked):
            records_by_id[sid] = _build_pending_stage_record(stage)
            blocked.add(sid)
            outputs_so_far.pop(sid, None)
            _flush_manifest(manifest, records_by_id, ordered, ctx, run_dir, RunStatus.RUNNING)
            continue

        # Skip stages already produced (resume path).
        if _stage_output_already_produced(sid, outputs_so_far, records_by_id):
            continue

        outcome, joins_blocked = _run_stage(stage, ctx, outputs_so_far, records_by_id, manifest, ordered, run_dir)
        if joins_blocked:
            blocked.add(sid)
        if outcome is _StageOutcome.HALTED:
            halted_stage_ids.append(sid)
        elif outcome is _StageOutcome.CANCELLED:
            cancelled = True
            cancel_at_index = idx
            break

    return _finalize_run_manifest(
        manifest, records_by_id, ordered, ctx, run_dir, cancelled, cancel_at_index, halted_stage_ids
    )


# --- _execute_stages helpers -------------------------------------------------


class _StageOutcome(enum.Enum):
    """What `_run_stage` learned about the stage it just ran, for the loop to
    act on. `RAN` covers `ok`, `validation_warnings`, and `error` alike —
    none of those need anything beyond letting the loop move to the next
    stage. `HALTED` additionally needs the loop to remember this stage id for
    `halted_at`. `CANCELLED` needs the loop to stop."""

    RAN = "ran"
    HALTED = "halted"
    CANCELLED = "cancelled"


class StageErrorInfo(TypedDict):
    """A stage's `error` field once it has failed: the exception's type name,
    a human-readable message, and its traceback — `None` for a
    row-generation error, which has no single exception to format."""

    type: str
    message: str
    traceback: str | None


class StageRecord(TypedDict):
    """One stage's manifest record, as written verbatim into
    `manifest["stages"]` and read back by the web layer — a plain
    TypedDict-typed dict, not a dataclass/model, since it is JSON on disk.
    `input_validation`/`output_validation` hold `ValidationReport.to_dict()`
    output (app.runtime.validation); that report's own fields are untyped
    (`dict[str, Any]`) in its own module, so `dict[str, object]` here is as
    precise a type as its actual contents support without retyping
    `ValidationReport` itself. `finished_at`, `output_path`, `queue_path`, and
    `notes` are added only at specific points in a stage's lifecycle
    (`_start_stage_record`'s initial dict omits `finished_at`;
    `_finalize_stage_output` adds `output_path`; `_record_halt` adds
    `queue_path`; `_apply_row_slicing`/`_persist_stage_output` add `notes` on
    the first trim/fallback; `_finalize_stage_output` adds `llm_usage` — the
    dumped `LlmUsage` model — when the stage recorded any) and are absent
    before then."""

    stage_id: str
    type: StageType
    name: str
    status: StageStatus
    input_validation: list[dict[str, object]]
    output_validation: dict[str, object] | None
    elapsed_ms: int
    rows: int
    error: StageErrorInfo | None
    started_at: str | None
    finished_at: NotRequired[str | None]
    output_path: NotRequired[str]
    queue_path: NotRequired[str]
    notes: NotRequired[list[str]]
    llm_usage: NotRequired[dict[str, object]]


def create_run_manifest(
    ordered: list[Stage],
    *,
    run_id: str,
    project: str,
    workflow_version: str,
    run_bindings: dict[str, dict[str, Any]],
    input_bindings: dict[str, dict[str, Any]],
    limits: dict[str, int],
    offsets: dict[str, int],
) -> dict[str, Any]:
    """The initial production run manifest — every stage pending, status running.
    The single source of the run-manifest shape: prepare_run mints it here and
    persists it with write_manifest rather than hand-building the dict, so the
    shape lives with the engine that later updates it (_flush_manifest /
    _finalize_run_manifest)."""
    return {
        "run_id": run_id,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "workflow_version": workflow_version,
        "limit_overrides": limits,
        "offset_overrides": offsets,
        "run_bindings": run_bindings,
        "input_bindings": input_bindings,
        "status": RunStatus.RUNNING,
        "stages": [_build_pending_stage_record(s) for s in ordered],
    }


def write_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    """The single writer of run_dir/manifest.json. The initial write (prepare_run),
    every mid-run flush, and finalization all persist through here."""
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8"
    )


def _build_pending_stage_record(stage: Stage) -> StageRecord:
    """A stage's manifest record before it has started, or once it has been
    marked blocked: `pending` status, no output, no timing."""
    return {
        "stage_id": stage.id, "type": stage.type, "name": stage.name,
        "status": StageStatus.PENDING, "input_validation": [], "output_validation": None,
        "elapsed_ms": 0, "rows": 0, "error": None,
        "started_at": None, "finished_at": None,
    }


def _flush_manifest(
    manifest: dict[str, Any],
    records_by_id: dict[str, StageRecord],
    ordered: list[Stage],
    ctx: RunContext,
    run_dir: Path,
    status: RunStatus,
) -> None:
    """Write the manifest mid-run so the run page can show live progress
    (stages light up as they start/finish) instead of the whole pipeline
    running silently and updating only at the very end."""
    m = dict(manifest)
    m["stages"] = [records_by_id.get(s.id) or _build_pending_stage_record(s) for s in ordered]
    m["status"] = status
    m["queue_stats"] = ctx.queue_stats
    m["dropped_columns"] = ctx.dropped_columns
    m["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        write_manifest(run_dir, m)
    except OSError:
        pass


def _start_stage_record(stage: Stage) -> StageRecord:
    """A stage's manifest record at the moment it starts running."""
    return {
        "stage_id": stage.id,
        "type": stage.type,
        "name": stage.name,
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "status": StageStatus.RUNNING,
        "input_validation": [],
        "output_validation": None,
        "elapsed_ms": 0,
        "rows": 0,
        "error": None,
    }


def _gather_stage_inputs(
    stage: Stage, outputs_so_far: dict[str, pd.DataFrame], record: StageRecord
) -> dict[str, pd.DataFrame]:
    """This stage's input dataframes, keyed by producer id, rejecting exact
    duplicate rows and recording an input-schema validation report for every
    input that declares one. Raises if an upstream output is missing — the
    caller's exception handling turns that into this stage's own error."""
    sid = stage.id
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
    return inputs_for_stage

def _resolve_handler(stage_type: StageType) -> StageHandler:
    handler = HANDLERS.get(stage_type)
    if handler is None:
        raise ValueError(f"No handler for stage type '{stage_type}'")
    return handler


def _record_halt(record: StageRecord, halt: HaltForReview, run_dir: Path) -> None:
    """Fork-blocking, not loop-ending: this stage awaits review and blocks
    its downstream, while independent forks keep running."""
    record["status"] = StageStatus.AWAITING_REVIEW
    record["rows"] = halt.pending_count
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record["queue_path"] = halt.queue_path.relative_to(run_dir).as_posix()


def _record_stage_error(record: StageRecord, exc: Exception) -> None:
    """Record any stage failure (a handler can raise ValueError, RuntimeError,
    a pandas/pyarrow error, etc.) in the manifest. This outcome always joins
    the caller's `blocked` set, so its transitive downstream is skipped and
    never marked `ok` on this stage's absent output."""
    record["status"] = StageStatus.ERROR
    record["error"] = {
        "type": type(exc).__name__,
        "message": str(exc),
        "traceback": traceback.format_exc(limit=8),
    }


def _apply_row_slicing(
    output: pd.DataFrame, stage: Stage, ctx: RunContext, record: StageRecord
) -> pd.DataFrame:
    """Offset then cap `output`'s rows, in the handler's emitted order. Offset
    (per-run only, from --offset stage=M) drops the first M rows; the cap
    (--limit stage=N, else the stage's static `limit:`) then keeps the first
    N. Used to throttle/page the expensive LLM fan-out. Each trim actually
    taken is recorded as a note on `record`."""
    sid = stage.id
    offset = ctx.offsets.get(sid)
    if isinstance(offset, int) and offset > 0 and len(output) > 0:
        record.setdefault("notes", []).append(
            f"offset={offset}: dropped first {min(offset, len(output))} of {len(output)} row(s)"
        )
        output = output.iloc[offset:].reset_index(drop=True).copy()
    limit = ctx.limits.get(sid, stage.limit)
    if isinstance(limit, int) and limit >= 0 and len(output) > limit:
        record.setdefault("notes", []).append(
            f"limit={limit}: truncated from {len(output)} to {limit} row(s)"
        )
        output = output.head(limit).copy()
    return output


def _persist_stage_output(output: pd.DataFrame, sid: str, run_dir: Path, record: StageRecord) -> Path:
    """Write `output` as the stage's parquet artifact, falling back to CSV
    (noting the fallback on `record`) for a column whose dtype/shape parquet
    can't represent — mixed-type object columns, nested Python values. A
    disk/OS error is NOT caught here: it would fail identically for CSV, so
    it propagates to the stage's own error handling rather than silently
    degrading the output."""
    output_path = run_dir / "outputs" / f"{sid}.parquet"
    try:
        output.to_parquet(output_path, index=False)
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        output_path = run_dir / "outputs" / f"{sid}.csv"
        output.to_csv(output_path, index=False)
        record.setdefault("notes", []).append(f"Wrote CSV instead of parquet: {exc}")
    return output_path


def _finalize_stage_output(
    stage: Stage,
    ctx: RunContext,
    record: StageRecord,
    output: pd.DataFrame | None,
    outputs_so_far: dict[str, pd.DataFrame],
    run_dir: Path,
) -> bool:
    """Trim, validate, and persist a stage's raw handler output, then decide
    its terminal status. A per-row generation failure is a stage error,
    recorded exactly like a raised exception — the partial output file stays
    on disk for inspection, and the stage's own `error` status keeps a resume
    from reusing it. Otherwise the status is `ok`/`validation_warnings` from
    the output and input validation reports. Returns True (a row-generation
    error) if the caller must join this stage to `blocked`, so every
    transitive consumer is skipped rather than run on this stage's partial
    frame and marked `ok`; False otherwise."""
    sid = stage.id
    if output is None:
        output = pd.DataFrame()
    output = _apply_row_slicing(output, stage, ctx, record)

    out_rep = validate_dataframe(output, stage.output_schema, stage_id=sid, phase="output")
    row_errors = ctx.row_errors.get(sid, [])
    if row_errors:
        out_rep.issues[0:0] = [
            Issue("error", None,
                  f"row {row_error['row']}: generation failed: {row_error['message']}")
            for row_error in row_errors
        ]
    record["output_validation"] = out_rep.to_dict()

    usage = ctx.llm_usage.get(sid)
    if usage is not None:
        # Dump to a plain dict here: the manifest is JSON, and this is
        # the one boundary where the typed LlmUsage becomes storage.
        record["llm_usage"] = usage.model_dump()

    output_path = _persist_stage_output(output, sid, run_dir, record)
    outputs_so_far[sid] = output

    if row_errors:
        record["status"] = StageStatus.ERROR
        record["error"] = {
            "type": "RowGenerationError",
            "message": _summarize_row_errors(row_errors),
            "traceback": None,
        }
    else:
        record["status"] = StageStatus.OK if out_rep.ok and all(
            v["ok"] for v in record["input_validation"]
        ) else StageStatus.VALIDATION_WARNINGS
    record["rows"] = int(len(output))
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record["output_path"] = output_path.relative_to(run_dir).as_posix()
    return bool(row_errors)


def _run_stage(
    stage: Stage,
    ctx: RunContext,
    outputs_so_far: dict[str, pd.DataFrame],
    records_by_id: dict[str, StageRecord],
    manifest: dict[str, Any],
    ordered: list[Stage],
    run_dir: Path,
) -> tuple[_StageOutcome, bool]:
    """Run one stage end to end: gather its inputs, invoke its handler,
    process and persist its output, and record the outcome (ok, warnings,
    error, halt, or a mid-stage cancel) into `records_by_id[stage.id]` —
    flushing the manifest once the stage starts and again once it settles, so
    the run page shows it live. Returns `(outcome, joins_blocked)`:
    `joins_blocked` is True for a halt, a general exception, and a
    row-generation error alike — every outcome except a clean ok/warnings or
    a cancel — so the caller can add this stage to its own `blocked` set
    itself, keeping that decision visible at the loop."""
    sid = stage.id
    record = _start_stage_record(stage)
    t0 = time.perf_counter()
    records_by_id[sid] = record
    _flush_manifest(manifest, records_by_id, ordered, ctx, run_dir, RunStatus.RUNNING)

    joins_blocked = False
    try:
        inputs_for_stage = _gather_stage_inputs(stage, outputs_so_far, record)
        handler = _resolve_handler(stage.type)
        try:
            output = handler.execute(stage, inputs_for_stage, ctx)
        except HaltForReview as halt:
            _record_halt(record, halt, run_dir)
            return _StageOutcome.HALTED, True
        except RunCancelled:
            # Mid-stage cancel: the row driver unwound out of
            # handler.execute (see execution.py::_run_row_mapper). This
            # stage made no output — it is marked cancelled, not ok.
            record["status"] = StageStatus.CANCELLED
            return _StageOutcome.CANCELLED, False
        joins_blocked = _finalize_stage_output(stage, ctx, record, output, outputs_so_far, run_dir)
    except Exception as exc:  # noqa: BLE001 — the runner's contract is to
        # record ANY stage failure in the manifest and keep running
        # independent forks rather than crash the whole run.
        _record_stage_error(record, exc)
        joins_blocked = True
    finally:
        # Every terminal status (ok, error, awaiting_review, cancelled)
        # finalizes its timing here — `record` is already in records_by_id
        # by reference, so the branches above set only their distinguishing
        # fields (status, halt queue info).
        record["elapsed_ms"] = int((time.perf_counter() - t0) * 1000)
        record["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _flush_manifest(manifest, records_by_id, ordered, ctx, run_dir, RunStatus.RUNNING)

    return _StageOutcome.RAN, joins_blocked


def _finalize_run_manifest(
    manifest: dict[str, Any],
    records_by_id: dict[str, StageRecord],
    ordered: list[Stage],
    ctx: RunContext,
    run_dir: Path,
    cancelled: bool,
    cancel_at_index: int,
    halted_stage_ids: list[str],
) -> dict[str, Any]:
    """Assemble and persist the run's final manifest once the loop has
    stopped, in topological order (blocked downstream stages were already
    marked `pending` inline, so no post-loop fill is needed). A cancel is a
    hard stop: it keeps the cancelled outcome regardless of any error/halt a
    stage recorded before the cancel arrived, and carries no `halted_at`, so
    a cancelled run never shows the review banner for a halt that happened
    earlier in the same run."""
    manifest["stages"] = [records_by_id[s.id] for s in ordered if s.id in records_by_id]
    manifest["finished_at"] = datetime.now().isoformat(timespec="seconds")
    manifest["queue_stats"] = ctx.queue_stats
    manifest["dropped_columns"] = ctx.dropped_columns

    if cancelled:
        manifest["status"] = RunStatus.CANCELLED
        manifest["cancelled_at"] = ordered[cancel_at_index].id
        manifest.pop("halted_at", None)
    else:
        if halted_stage_ids:
            manifest["halted_at"] = halted_stage_ids
        else:
            manifest.pop("halted_at", None)
        manifest["status"] = _final_run_status(
            record["status"] for record in manifest["stages"]
        )

    write_manifest(run_dir, manifest)
    return manifest


# --- loop decision helpers ---------------------------------------------------


def _summarize_row_errors(row_errors: list[RowError]) -> str:
    """One-line summary of per-row generation failures for the stage's error
    record — the per-row detail lives in output_validation issues."""
    head = "; ".join(f"row {e['row']}: {e['message']}" for e in row_errors[:3])
    more = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
    return f"{len(row_errors)} row(s) failed generation: {head}{more}"


def _read_run_identity(ctx: RunContext) -> RunIdentity | None:
    """This run's logical identity, carried on `ctx.identity` by
    prepare_run/resume_run for cancellation's checkpoints. None for a
    subset/eval run's ctx (built by _subset_ctx), which carries no identity —
    those runs are simply not cancellable."""
    return ctx.identity


def _consume_cancel(ctx: RunContext) -> bool:
    """Consume this run's cancel message if one is pending — read-once, so a
    True means one was pending and is now gone (see _read_run_identity for when
    a run is cancellable at all)."""
    identity = _read_run_identity(ctx)
    return identity is not None and consume_cancel(identity.project, identity.run_id)


def _find_blocking_upstream(stage: Stage, blocked: set[str]) -> list[str]:
    """Input-producer stage ids in `blocked` — producers that errored, halted,
    or are themselves downstream of one. Non-empty means this stage cannot run
    on real inputs and must be skipped; empty means every producer succeeded.
    Topological order guarantees every producer has been processed before its
    consumer, so membership in `blocked` is decided by the time it is read."""
    return [input_id for input_id in stage.input_ids if input_id in blocked]


def _stage_output_already_produced(
    sid: str, outputs_so_far: dict[str, pd.DataFrame], records_by_id: dict[str, StageRecord]
) -> bool:
    """True when `sid`'s output was computed in a prior partial run (the
    resume path) and its last recorded status is a completion the loop can
    trust to skip re-running it, rather than a stale record from before a
    halt/cancel/error."""
    if sid not in outputs_so_far:
        return False
    record = records_by_id.get(sid)
    return record is not None and record["status"] in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


def _final_run_status(stage_statuses: Iterable[str]) -> RunStatus:
    """A non-cancelled run's overall status from its stages' statuses, error-first:
    any errored stage -> errors; else any halted stage -> awaiting_review; else
    any warnings -> warnings; else ok. A `pending` (blocked) stage only exists
    downstream of an errored/halted one, so it never needs a branch of its own."""
    statuses = set(stage_statuses)
    if StageStatus.ERROR in statuses:
        return RunStatus.ERRORS
    if StageStatus.AWAITING_REVIEW in statuses:
        return RunStatus.AWAITING_REVIEW
    if StageStatus.VALIDATION_WARNINGS in statuses:
        return RunStatus.WARNINGS
    return RunStatus.OK


# --- duplicate-input-row rejection (every stage type) ------------------------


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
