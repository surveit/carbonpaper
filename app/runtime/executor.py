"""Shared stage-execution engine, driven both by the production run lifecycle
(`app/runtime/runner.py`) and by `run_subset` here, the non-production subset
executor used by evals and previews. This module never creates a production
run record; that split is what lets an import-linter contract keep evals away
from the production run entry points."""

from __future__ import annotations

import enum
import hashlib
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.errors import SubsetRunError
from app.models import Stage, StageType, Workflow
from app.core.run_status import RunStatus, StageStatus

from .cancellation import consume_cancel
from .context import RunContext, RunIdentity
from .errors import RunCancelled
from .manifest import (
    CONTRIBUTION_ATTR,
    RowError,
    RunManifest,
    StageContribution,
    StageErrorInfo,
    StageRecord,
    create_run_manifest,
    write_manifest,
)
from .run_log import RUN_START, STAGE_DONE, STAGE_START, RunLog
from .stages import HANDLERS, HaltForReview, StageHandler
from .lineage import (
    LINEAGE_ATTR,
    RowLineage,
    concatenated_inputs_lineage,
    lineage_sidecar_path,
    read_row_lineage,
)
from .validation import Issue, Severity, ValidationReport, validate_dataframe


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
    queue_auto_approve: bool = False,
    project: str | None = None,
    workflow_version: str | None = None,
    identity: RunIdentity | None = None,
    is_test_run: bool = False,
) -> dict[str, pd.DataFrame]:
    """Raises SubsetRunError if a stage errors or the run halts — never a half-populated output dict."""
    by_id = workflow.index_stages_by_id()
    missing = [sid for sid in stage_ids if sid not in by_id]
    if missing:
        raise SubsetRunError(f"subset names stage(s) not in the workflow: {missing}")
    ordered = topological_sort([by_id[sid] for sid in stage_ids])
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    manifest = create_run_manifest(
        ordered, run_id=run_dir.name, project=project,
        workflow_version=workflow_version, run_bindings={}, input_bindings={},
        limits={}, offsets={}, bust_cache=False, is_test_run=is_test_run)
    write_manifest(run_dir, manifest)
    outputs: dict[str, pd.DataFrame] = dict(injected_outputs)
    manifest = _execute_stages(
        ordered, _subset_ctx(repo_root, run_dir, queue_auto_approve, identity),
        manifest, run_dir, outputs)
    _raise_if_run_failed(manifest)
    return outputs


def _subset_ctx(
    repo_root: Path, run_dir: Path, queue_auto_approve: bool, identity: RunIdentity | None
) -> RunContext:
    # `identity` is what makes this a workflow test's run rather than a bare
    # subset: it grants project scope, read-only. Without it a subset run is keyed
    # on the Workflow + run_dir, not a project tree, and has no cache access — a
    # handler that needs project scope (human_review_queue, or a publish stage's
    # trace_links) then fails loudly rather than reading a fabricated wrong
    # directory, unless `queue_auto_approve` tells human_review_queue to pass rows
    # through in memory instead.
    if identity is not None:
        return RunContext.for_workflow_test_run(
            repo_root, run_dir, identity.project, identity.run_id)
    return RunContext.for_stages_outside_a_run(
        repo_root, run_dir, queue_auto_approve=queue_auto_approve)


def _raise_if_run_failed(manifest: RunManifest) -> None:
    status = manifest.status
    if status in (RunStatus.OK, RunStatus.WARNINGS):
        return
    if status == RunStatus.AWAITING_REVIEW:
        halted_at = ", ".join(manifest.halted_at or [])
        raise SubsetRunError(f"run halted for human review at {halted_at}")
    for stage in manifest.stage_records:
        if stage.status == StageStatus.ERROR:
            message = stage.error.message if stage.error is not None else "unknown error"
            raise SubsetRunError(f"stage {stage.stage_id!r} errored: {message}")
    raise SubsetRunError(f"run did not complete (status {status!r})")


def _execute_stages(
    ordered: list[Stage],
    ctx: RunContext,
    manifest: RunManifest,
    run_dir: Path,
    outputs_so_far: dict[str, pd.DataFrame],
) -> RunManifest:
    # Opened here, not in the RunContext constructors, so EVERY entry path
    # (run_prepared, execute_run, run_subset, resume_run) is logged regardless of
    # the ctx it built, and the log's lifetime is exactly this call's.
    run_log = RunLog(run_dir / "events.jsonl")
    run_log.emit({
        "kind": RUN_START, "run_id": manifest.run_id, "stage_count": len(ordered),
    })
    try:
        return _run_ordered_stages(
            ordered, ctx.attach_run_log(run_log), manifest, run_dir, outputs_so_far
        )
    finally:
        # close() writes the terminal run_done marker the SSE tailer stops on —
        # in a finally, so an exception escaping the loop still ends the stream
        # instead of leaving a client tailing forever.
        run_log.close()


def _run_ordered_stages(
    ordered: list[Stage],
    ctx: RunContext,
    manifest: RunManifest,
    run_dir: Path,
    outputs_so_far: dict[str, pd.DataFrame],
) -> RunManifest:
    """Error and halt are fork-blocking, not loop-ending: independent forks run to completion."""
    halted_stage_ids: list[str] = []
    blocked: set[str] = set()
    cancelled = False
    cancel_at_index: int = -1

    # Carry over any existing records (from a previously halted manifest
    # we're resuming). Build an index for upsert behavior.
    records_by_id: dict[str, StageRecord] = {
        r.stage_id: r for r in manifest.stage_records
    }
    _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)

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
            records_by_id[sid] = StageRecord.record_with_status(stage, StageStatus.PENDING)
            blocked.add(sid)
            outputs_so_far.pop(sid, None)
            _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)
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
        manifest, records_by_id, ordered, run_dir, cancelled, cancel_at_index, halted_stage_ids
    )


# --- _execute_stages helpers -------------------------------------------------


class _StageOutcome(enum.Enum):
    """RAN covers ok, validation_warnings, and error alike — all three just let the loop continue."""

    RAN = "ran"
    HALTED = "halted"
    CANCELLED = "cancelled"


def _flush_manifest(
    manifest: RunManifest,
    records_by_id: dict[str, StageRecord],
    ordered: list[Stage],
    run_dir: Path,
    status: RunStatus,
) -> None:
    """Writes a stamped snapshot without mutating the live manifest; an OSError is swallowed."""
    snapshot = manifest.model_copy(update={
        "stage_records": [
            records_by_id.get(s.id)
            or StageRecord.record_with_status(s, StageStatus.PENDING)
            for s in ordered
        ],
        "status": status,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    })
    try:
        write_manifest(run_dir, snapshot)
    except OSError:
        pass


def _gather_stage_inputs(
    stage: Stage, outputs_so_far: dict[str, pd.DataFrame], record: StageRecord
) -> dict[str, pd.DataFrame]:
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
            record.input_validation_report.append(rep.to_dict())
    return inputs_for_stage

def _resolve_handler(stage_type: StageType) -> StageHandler:
    handler = HANDLERS.get(stage_type)
    if handler is None:
        raise ValueError(f"No handler for stage type '{stage_type}'")
    return handler


def _record_halt(record: StageRecord, halt: HaltForReview, run_dir: Path) -> None:
    record.status = StageStatus.AWAITING_REVIEW
    record.output_row_count = halt.pending_count
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record.queue_path = halt.queue_path.relative_to(run_dir).as_posix()


def _record_stage_error(record: StageRecord, exc: Exception) -> None:
    record.status = StageStatus.ERROR
    record.error = StageErrorInfo(
        type=type(exc).__name__,
        message=str(exc),
        traceback=traceback.format_exc(limit=8),
    )


def _apply_row_slicing(
    output: pd.DataFrame, stage: Stage, ctx: RunContext, record: StageRecord
) -> tuple[pd.DataFrame, int, int | None]:
    """Returns the trimmed frame and the (start, stop) window it took out of the handler's rows."""
    sid = stage.id
    start = 0
    offset = ctx.offsets.get(sid)
    if isinstance(offset, int) and offset > 0 and len(output) > 0:
        record.add_note(
            f"offset={offset}: dropped first {min(offset, len(output))} of {len(output)} row(s)"
        )
        start = min(offset, len(output))
        output = output.iloc[offset:].reset_index(drop=True).copy()
    stop: int | None = None
    limit = ctx.limits.get(sid, stage.limit)
    if isinstance(limit, int) and limit >= 0 and len(output) > limit:
        record.add_note(
            f"limit={limit}: truncated from {len(output)} to {limit} row(s)"
        )
        stop = start + limit
        output = output.head(limit).copy()
    return output, start, stop


def _persist_row_lineage(lineage: RowLineage, sid: str, run_dir: Path) -> None:
    """Read by `app.runtime.trace` to cross a hop that isn't row-preserving by position alone."""
    lineage.to_frame().to_parquet(lineage_sidecar_path(run_dir, sid), index=False)


def _stage_row_lineage(
    stage: Stage, output: pd.DataFrame | None, inputs: dict[str, pd.DataFrame]
) -> RowLineage | None:
    """None where output row i is input row i and the trace needs no help crossing this stage."""
    driven = read_row_lineage(output)
    if driven is not None:
        return driven
    if stage.type == StageType.union:
        return concatenated_inputs_lineage(stage, inputs)
    return None


def _persist_stage_output(output: pd.DataFrame, sid: str, run_dir: Path, record: StageRecord) -> Path:
    """Falls back to CSV when parquet can't represent a column's dtype; a disk error is not caught."""
    output_path = run_dir / "outputs" / f"{sid}.parquet"
    try:
        output.to_parquet(output_path, index=False)
    except (pa_lib.ArrowException, ValueError, TypeError) as exc:
        output_path = run_dir / "outputs" / f"{sid}.csv"
        output.to_csv(output_path, index=False)
        record.add_note(f"Wrote CSV instead of parquet: {exc}")
    return output_path


def _finalize_stage_output(
    stage: Stage,
    ctx: RunContext,
    record: StageRecord,
    output: pd.DataFrame | None,
    inputs_for_stage: dict[str, pd.DataFrame],
    outputs_so_far: dict[str, pd.DataFrame],
    run_dir: Path,
    manifest: RunManifest,
) -> bool:
    """An error-severity output validation issue is a stage error. True = caller must join `blocked`."""
    sid = stage.id
    # Read the stage's contribution off the handler's frame BEFORE any slicing
    # (which builds a new frame and drops `.attrs`), then merge usage into this
    # record and human_review_queue_stats/dropped_columns into the manifest.
    contribution = _read_stage_contribution(output)
    row_errors = _merge_stage_contribution(contribution, sid, manifest, record)

    if output is None:
        output = pd.DataFrame()
    # Drop the contribution channel so it never reaches the persisted parquet
    # (its metadata isn't JSON-serializable) — it has been merged above.
    output.attrs.pop(CONTRIBUTION_ATTR, None)
    lineage = _stage_row_lineage(stage, output, inputs_for_stage)
    # Drop the lineage channel for the same reason as the contribution one
    # above: `.attrs` is not JSON-serializable and must not reach the parquet.
    output.attrs.pop(LINEAGE_ATTR, None)
    output, start, stop = _apply_row_slicing(output, stage, ctx, record)
    if lineage is not None:
        # Slicing happens after the handler emitted, so the lineage narrows with
        # it — entry i must still describe output row i.
        _persist_row_lineage(lineage.sliced(start, stop), sid, run_dir)

    out_rep = validate_dataframe(output, stage.output_schema, stage_id=sid, phase="output")
    if row_errors:
        out_rep.issues[0:0] = [
            Issue("error", None,
                  f"row {row_error['row']}: generation failed: {row_error['message']}")
            for row_error in row_errors
        ]
    record.output_validation_report = out_rep.to_dict()

    output_path = _persist_stage_output(output, sid, run_dir, record)
    outputs_so_far[sid] = output

    if row_errors:
        record.status = StageStatus.ERROR
        record.error = StageErrorInfo(
            type="RowGenerationError",
            message=_summarize_row_errors(row_errors),
            traceback=None,
        )
    elif not out_rep.ok:
        record.status = StageStatus.ERROR
        record.error = StageErrorInfo(
            type="OutputSchemaViolation",
            message=_summarize_output_schema_errors(sid, out_rep),
            traceback=None,
        )
    else:
        record.status = StageStatus.OK if all(
            v["ok"] for v in record.input_validation_report
        ) else StageStatus.VALIDATION_WARNINGS
    record.output_row_count = int(len(output))
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record.output_path = output_path.relative_to(run_dir).as_posix()
    return record.status == StageStatus.ERROR


def _read_stage_contribution(output: pd.DataFrame | None) -> StageContribution:
    if output is None:
        return StageContribution()
    attached = output.attrs.get(CONTRIBUTION_ATTR)
    if isinstance(attached, StageContribution):
        return attached
    return StageContribution()


def _merge_stage_contribution(
    contribution: StageContribution,
    sid: str,
    manifest: RunManifest,
    record: StageRecord,
) -> list[RowError]:
    for note in contribution.notes:
        record.add_note(note)
    if contribution.llm_usage is not None:
        record.llm_usage = contribution.llm_usage
    if contribution.dropped_columns:
        manifest.record_dropped_columns(sid, contribution.dropped_columns)
    if contribution.human_review_queue_stats is not None:
        manifest.record_human_review_queue_stats(
            sid, contribution.human_review_queue_stats)
    return contribution.row_errors


def _run_stage(
    stage: Stage,
    ctx: RunContext,
    outputs_so_far: dict[str, pd.DataFrame],
    records_by_id: dict[str, StageRecord],
    manifest: RunManifest,
    ordered: list[Stage],
    run_dir: Path,
) -> tuple[_StageOutcome, bool]:
    """Returns (outcome, joins_blocked); joins_blocked covers every outcome but clean ok and cancel."""
    sid = stage.id
    record = StageRecord.record_with_status(stage, StageStatus.RUNNING)
    t0 = time.perf_counter()
    records_by_id[sid] = record
    _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)
    _emit_stage_start(ctx.run_log, stage)

    joins_blocked = False
    try:
        inputs_for_stage = _gather_stage_inputs(stage, outputs_so_far, record)
        handler = _resolve_handler(stage.type)
        try:
            output = handler.execute(stage, inputs_for_stage, ctx)
        except HaltForReview as halt:
            # The halt fires before a frame is returned, so its contribution
            # (the stage's human_review_queue_stats) rides the exception; merge it into the
            # manifest exactly as a returned frame's would be.
            _merge_stage_contribution(halt.contribution, sid, manifest, record)
            _record_halt(record, halt, run_dir)
            return _StageOutcome.HALTED, True
        except RunCancelled:
            # Mid-stage cancel: the row driver unwound out of
            # handler.execute (see execution.py::_run_row_mapper). This
            # stage made no output — it is marked cancelled, not ok.
            record.status = StageStatus.CANCELLED
            return _StageOutcome.CANCELLED, False
        joins_blocked = _finalize_stage_output(
            stage, ctx, record, output, inputs_for_stage, outputs_so_far, run_dir,
            manifest)
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
        record.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        record.finished_at = datetime.now().isoformat(timespec="seconds")
        _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)
        _emit_stage_done(ctx.run_log, record)

    return _StageOutcome.RAN, joins_blocked


def _emit_stage_start(log: RunLog | None, stage: Stage) -> None:
    if log is not None:
        log.emit({
            "kind": STAGE_START, "stage": stage.id, "type": stage.type,
            "name": stage.name,
        })


def _emit_stage_done(log: RunLog | None, record: StageRecord) -> None:
    if log is not None:
        log.emit({
            "kind": STAGE_DONE, "stage": record.stage_id, "status": record.status,
            "rows": record.output_row_count, "elapsed_ms": record.elapsed_ms,
            "error": None if record.error is None else record.error.message,
        })


def _finalize_run_manifest(
    manifest: RunManifest,
    records_by_id: dict[str, StageRecord],
    ordered: list[Stage],
    run_dir: Path,
    cancelled: bool,
    cancel_at_index: int,
    halted_stage_ids: list[str],
) -> RunManifest:
    """A cancel is a hard stop: it wins over any earlier error/halt and carries no `halted_at`."""
    manifest.settle_stage_records(
        [records_by_id[s.id] for s in ordered if s.id in records_by_id])
    manifest.finished_at = datetime.now().isoformat(timespec="seconds")

    if cancelled:
        manifest.status = RunStatus.CANCELLED
        manifest.cancelled_at = ordered[cancel_at_index].id
        manifest.clear_halt()
    else:
        if halted_stage_ids:
            manifest.halted_at = halted_stage_ids
        else:
            manifest.clear_halt()
        manifest.status = _final_run_status(
            record.status for record in manifest.stage_records
        )

    write_manifest(run_dir, manifest)
    return manifest


# --- loop decision helpers ---------------------------------------------------


def _summarize_row_errors(row_errors: list[RowError]) -> str:
    head = "; ".join(f"row {e['row']}: {e['message']}" for e in row_errors[:3])
    more = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
    return f"{len(row_errors)} row(s) failed generation: {head}{more}"


def _summarize_output_schema_errors(sid: str, report: ValidationReport) -> str:
    errors = [issue for issue in report.issues if issue.severity == Severity.error]
    named = sorted({issue.column for issue in errors if issue.column})
    columns = f" (column(s): {', '.join(named)})" if named else ""
    return (
        f"stage '{sid}' output violates its declared output_schema{columns}: "
        + "; ".join(issue.message for issue in errors)
    )


def _read_run_identity(ctx: RunContext) -> RunIdentity | None:
    """None for a subset/eval ctx, which carries no identity — those runs are not cancellable."""
    return ctx.identity


def _consume_cancel(ctx: RunContext) -> bool:
    """Read-once: True means a cancel was pending and is now consumed."""
    identity = _read_run_identity(ctx)
    return identity is not None and consume_cancel(identity.project, identity.run_id)


def _find_blocking_upstream(stage: Stage, blocked: set[str]) -> list[str]:
    """Topological order guarantees every producer was processed before this read of `blocked`."""
    return [input_id for input_id in stage.input_ids if input_id in blocked]


def _stage_output_already_produced(
    sid: str, outputs_so_far: dict[str, pd.DataFrame], records_by_id: dict[str, StageRecord]
) -> bool:
    if sid not in outputs_so_far:
        return False
    record = records_by_id.get(sid)
    return record is not None and record.status in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


def _final_run_status(stage_statuses: Iterable[str]) -> RunStatus:
    """A `pending` stage only exists downstream of an errored/halted one, so it needs no branch."""
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
    """Identity is a content hash over every column; the declared primary_key plays no part."""
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
