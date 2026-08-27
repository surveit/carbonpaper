"""docs/architecture.md"""

from __future__ import annotations

import enum
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple

import pandas as pd
import pyarrow as pa

from app.core.errors import SubsetRunError
from app.core.frames import frame_to_table, write_frame_table_with_csv_fallback
from app.models import StageType, Workflow, WorkflowStage
from app.models.run_manifest import (
    SCHEMA_REFUSAL_ERROR_TYPE,
    StageErrorInfo,
    StageRecord,
)
from app.models.stage_contribution import RowError, StageContribution
from app.models.run_parameters import RunParameters
from app.core.run_status import RunStatus, StageStatus

from .cancellation import consume_cancel
from .context import RunContext, RunIdentity
from .workflow_outputs import find_workflow_output_issues, save_workflow_outputs
from .stage_output import AwaitingReview, StageOutput
from .errors import RunCancelled
from .manifest import RunManifest, create_run_manifest, write_manifest
from .run_log import RUN_START, STAGE_DONE, STAGE_START, RunLog
from .progress import StageProgressReporter
from .stages import HANDLERS, StageHandler
from .lineage import RowLineage, concatenated_inputs_lineage, kept_rows_lineage
from .lineage_sidecar import write_lineage_sidecar
from app.models.severity import UserFacingErrorSeverity
from .key_coverage import find_key_coverage_issues
from .validation import Issue, ValidationReport, validate_table


def topological_sort(stages: list[WorkflowStage]) -> list[WorkflowStage]:
    by_id = {s.id: s for s in stages}
    visited: set[str] = set()
    order: list[WorkflowStage] = []

    def visit(sid: str, path: list[str]) -> None:
        if sid in visited:
            return
        if sid in path:
            raise ValueError(f"Cycle detected: {' → '.join(path + [sid])}")
        for iid in by_id[sid].stage.input_ids:
            if iid in by_id:
                visit(iid, path + [sid])
        visited.add(sid)
        order.append(by_id[sid])

    for sid in by_id:
        visit(sid, [])
    return order


def execute_subset(
    workflow: Workflow,
    *,
    injected_outputs: dict[str, pd.DataFrame],
    stage_ids: list[str],
    run_dir: Path,
    params: RunParameters = RunParameters(),
    project_id: str,
    workflow_version: str | None = None,
    identity: RunIdentity | None = None,
) -> dict[str, pd.DataFrame]:
    """Raises SubsetRunError on any stage error or halt — callers get a full output set or none."""
    by_id = workflow.index_workflow_stages_by_id()
    missing = [sid for sid in stage_ids if sid not in by_id]
    if missing:
        raise SubsetRunError(f"subset names stage(s) not in the workflow: {missing}")
    ordered = topological_sort([by_id[sid] for sid in stage_ids])
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    ctx = _subset_ctx(run_dir, identity, params)
    manifest = create_run_manifest(
        ordered, ctx, run_id=run_dir.name, project_id=project_id,
        workflow_version=workflow_version, input_bindings={},
        # The dir the run lives under is what separates a production run from an
        # eval one; the record keeps that separation.
        area=run_dir.parent.name)
    write_manifest(manifest)
    outputs: dict[str, pa.Table] = {
        sid: frame_to_table(frame) for sid, frame in injected_outputs.items()
    }
    manifest = _execute_stages(ordered, ctx, manifest, run_dir, outputs)
    _raise_if_run_failed(manifest)
    return outputs


def _subset_ctx(
    run_dir: Path, identity: RunIdentity | None, params: RunParameters,
) -> RunContext:
    if identity is not None:
        return RunContext.for_workflow_test_run(
            run_dir, identity.project, identity.run_id, params)
    return RunContext.for_stages_outside_a_run(run_dir, params)


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
    ordered: list[WorkflowStage],
    ctx: RunContext,
    manifest: RunManifest,
    run_dir: Path,
    outputs_so_far: dict[str, pa.Table],
) -> RunManifest:
    run_log = RunLog(manifest.project, manifest.run_id)
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
    ordered: list[WorkflowStage],
    ctx: RunContext,
    manifest: RunManifest,
    run_dir: Path,
    outputs_so_far: dict[str, pa.Table],
) -> RunManifest:
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
            records_by_id[sid] = StageRecord.record_with_status(
                stage.stage, StageStatus.PENDING)
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
    """RAN covers `ok`, `validation_warnings` and `error` alike — the loop treats all three the same."""

    RAN = "ran"
    HALTED = "halted"
    CANCELLED = "cancelled"


def _flush_manifest(
    manifest: RunManifest,
    records_by_id: dict[str, StageRecord],
    ordered: list[WorkflowStage],
    run_dir: Path,
    status: RunStatus,
) -> None:
    snapshot = manifest.model_copy(update={
        "stage_records": [
            records_by_id.get(s.id)
            or StageRecord.record_with_status(s.stage, StageStatus.PENDING)
            for s in ordered
        ],
        "status": status,
    })
    write_manifest(snapshot)


def _gather_stage_inputs(
    workflow_stage: WorkflowStage, outputs_so_far: dict[str, pa.Table],
    ctx: RunContext, record: StageRecord,
) -> tuple[dict[str, pa.Table], _RowWindow]:
    """Cuts the row window BEFORE the schema checks, so a limit of 3 isn't failed by row 4,000."""
    sid = workflow_stage.id
    window = _resolve_row_window(workflow_stage, ctx)
    inputs_for_stage: dict[str, pa.Table] = {}
    for ref in workflow_stage.inputs:
        if ref.id not in outputs_so_far:
            raise RuntimeError(f"Upstream stage '{ref.id}' has no output yet")
        table = _take_row_window(
            outputs_so_far[ref.id], window, f"from input '{ref.id}'", record)
        inputs_for_stage[ref.id] = table
        report = validate_table(
            table, ref.table_schema, stage_id=sid, phase=f"input:{ref.id}")
        record.input_validation_report.append(report.to_dict())
    return inputs_for_stage, window


def _resolve_handler(stage_type: StageType) -> StageHandler:
    handler = HANDLERS.get(stage_type)
    if handler is None:
        raise ValueError(f"No handler for stage type '{stage_type}'")
    return handler


def _record_halt(record: StageRecord, halt: AwaitingReview, run_dir: Path) -> None:
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


class _RowWindow(NamedTuple):
    """`start` is the ordinal, in the upstream rows, of the first row the stage reads."""

    start: int
    cap: int | None


def _resolve_row_window(workflow_stage: WorkflowStage, ctx: RunContext) -> _RowWindow:
    offset = ctx.params.offsets.get(workflow_stage.id)
    cap = ctx.params.limits.get(workflow_stage.id)
    return _RowWindow(
        offset if isinstance(offset, int) and offset > 0 else 0,
        cap if isinstance(cap, int) and cap >= 0 else None,
    )


def _take_row_window(
    table: pa.Table, window: _RowWindow, source: str, record: StageRecord
) -> pa.Table:
    """`window` of `table`'s rows; every cut actually taken is noted on `record`, never silent."""
    if window.start > 0 and len(table) > 0:
        record.add_note(
            f"offset={window.start}: skipped the first "
            f"{min(window.start, len(table))} of {len(table)} row(s) {source}"
        )
        table = table.slice(window.start)
    if window.cap is not None and len(table) > window.cap:
        record.add_note(f"limit={window.cap}: read {window.cap} of {len(table)} row(s) {source}")
        table = table.slice(0, window.cap)
    return table


def _stage_row_lineage(
    workflow_stage: WorkflowStage, output: StageOutput,
    inputs: dict[str, pa.Table], window: _RowWindow,
) -> RowLineage | None:
    """None means output row i is input row i, so the trace needs no help crossing this stage."""
    if output.lineage is not None:
        # A no-input stage originates its rows, so a window cuts them, never moves them.
        if not workflow_stage.inputs:
            return output.lineage.sliced(window.start, window.cap)
        return output.lineage.shifted(window.start)
    if workflow_stage.stage.type == StageType.union:
        return concatenated_inputs_lineage(workflow_stage, inputs, window.start)
    return _sliced_input_lineage(workflow_stage, output.table, window)


def _sliced_input_lineage(
    workflow_stage: WorkflowStage, output: pa.Table | None, window: _RowWindow
) -> RowLineage | None:
    if window.start == 0 and window.cap is None:
        return None
    stage = workflow_stage.stage
    if not stage.is_grain_and_order_preserving or len(workflow_stage.inputs) != 1:
        return None
    rows = 0 if output is None else len(output)
    return kept_rows_lineage(
        workflow_stage.inputs[0].id, list(range(window.start, window.start + rows)))


def _persist_stage_output(output: pa.Table, sid: str, run_dir: Path, record: StageRecord) -> Path:
    """The stage's artifact path, after writing it — the CSV fallback is NOTED on `record`."""
    written = write_frame_table_with_csv_fallback(
        output, run_dir / "outputs" / f"{sid}.parquet"
    )
    if written.parquet_error is not None:
        record.add_note(f"Wrote CSV instead of parquet: {written.parquet_error}")
    return written.path


def _finalize_stage_output(
    workflow_stage: WorkflowStage,
    window: _RowWindow,
    record: StageRecord,
    output: StageOutput | None,
    inputs_for_stage: dict[str, pa.Table],
    outputs_so_far: dict[str, pa.Table],
    run_dir: Path,
    manifest: RunManifest,
    ctx: RunContext,
) -> bool:
    sid = workflow_stage.id
    if output is None:
        output = StageOutput(pa.table({}))
    row_errors = _merge_stage_contribution(output.contribution, sid, manifest, record)
    lineage = _stage_row_lineage(workflow_stage, output, inputs_for_stage, window)
    table = output.table
    if not workflow_stage.inputs:
        # A stage with no inputs originates its rows outside the run, so the
        # frame it just loaded is the runtime's only handle on them: its window
        # is taken here rather than on an input frame that does not exist.
        table = _take_row_window(table, window, "loaded from the source", record)
    write_lineage_sidecar(run_dir, sid, lineage, output.branches)

    out_rep = validate_table(
        table, workflow_stage.output_schema, stage_id=sid, phase="output")
    out_rep.issues.extend(find_key_coverage_issues(workflow_stage, inputs_for_stage))
    out_rep.issues.extend(find_workflow_output_issues(workflow_stage, table))
    if row_errors:
        out_rep.issues[0:0] = [
            Issue("error", None,
                  f"row {row_error['row']}: generation failed: {row_error['message']}")
            for row_error in row_errors
        ]
    record.output_validation_report = out_rep.to_dict()

    output_path = _persist_stage_output(table, sid, run_dir, record)
    outputs_so_far[sid] = table

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
            type=SCHEMA_REFUSAL_ERROR_TYPE,
            message=_summarize_output_schema_errors(sid, out_rep),
            traceback=None,
        )
    else:
        record.status = StageStatus.OK if all(
            v["ok"] for v in record.input_validation_report
        ) else StageStatus.VALIDATION_WARNINGS
    record.output_row_count = table.num_rows
    if record.status != StageStatus.ERROR and ctx.identity is not None:
        save_workflow_outputs(workflow_stage, table, ctx.identity)
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record.output_path = output_path.relative_to(run_dir).as_posix()
    return record.status == StageStatus.ERROR


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
    if contribution.cached_rows is not None:
        record.cached_rows = contribution.cached_rows
    if contribution.dropped_columns:
        manifest.record_dropped_columns(sid, contribution.dropped_columns)
    if contribution.human_review_queue_stats is not None:
        manifest.record_human_review_queue_stats(
            sid, contribution.human_review_queue_stats)
    return contribution.row_errors


def _run_stage(
    workflow_stage: WorkflowStage,
    ctx: RunContext,
    outputs_so_far: dict[str, pa.Table],
    records_by_id: dict[str, StageRecord],
    manifest: RunManifest,
    ordered: list[WorkflowStage],
    run_dir: Path,
) -> tuple[_StageOutcome, bool]:
    stage = workflow_stage.stage
    sid = stage.id
    record = StageRecord.record_with_status(stage, StageStatus.RUNNING)
    t0 = time.perf_counter()
    records_by_id[sid] = record
    _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)
    _emit_stage_start(ctx.run_log, workflow_stage)
    progress = StageProgressReporter(
        record,
        lambda: _flush_manifest(
            manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING
        ),
    )
    stage_ctx = ctx.attach_stage_progress(progress)

    joins_blocked = False
    try:
        inputs_for_stage, window = _gather_stage_inputs(
            workflow_stage, outputs_so_far, stage_ctx, record)
        handler = _resolve_handler(stage.type)
        try:
            output = handler.execute(workflow_stage, inputs_for_stage, stage_ctx)
        except RunCancelled:
            # Mid-stage cancel: the row driver unwound out of
            # handler.execute (see execution.py::_run_row_mapper). This
            # stage made no output — it is marked cancelled, not ok.
            record.status = StageStatus.CANCELLED
            return _StageOutcome.CANCELLED, False
        if output is not None and output.awaiting_review is not None:
            _merge_stage_contribution(output.contribution, sid, manifest, record)
            _record_halt(record, output.awaiting_review, run_dir)
            return _StageOutcome.HALTED, True
        joins_blocked = _finalize_stage_output(
            workflow_stage, window, record, output, inputs_for_stage, outputs_so_far,
            run_dir, manifest, ctx)
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
        progress.finish()
        _emit_stage_done(ctx.run_log, record)

    return _StageOutcome.RAN, joins_blocked


def _emit_stage_start(log: RunLog | None, workflow_stage: WorkflowStage) -> None:
    if log is not None:
        log.emit({
            "kind": STAGE_START, "stage": workflow_stage.id,
            "type": workflow_stage.stage.type,
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
    ordered: list[WorkflowStage],
    run_dir: Path,
    cancelled: bool,
    cancel_at_index: int,
    halted_stage_ids: list[str],
) -> RunManifest:
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

    write_manifest(manifest)
    return manifest


# --- loop decision helpers ---------------------------------------------------


def _summarize_row_errors(row_errors: list[RowError]) -> str:
    head = "; ".join(f"row {e['row']}: {e['message']}" for e in row_errors[:3])
    more = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
    return f"{len(row_errors)} row(s) failed generation: {head}{more}"


def _summarize_output_schema_errors(sid: str, report: ValidationReport) -> str:
    errors = [issue for issue in report.issues if issue.severity == UserFacingErrorSeverity.error]
    named = sorted({issue.column for issue in errors if issue.column})
    columns = f" (column(s): {', '.join(named)})" if named else ""
    return (
        f"stage '{sid}' output violates its output schema{columns}: "
        + "; ".join(issue.message for issue in errors)
    )


def _read_run_identity(ctx: RunContext) -> RunIdentity | None:
    return ctx.identity


def _consume_cancel(ctx: RunContext) -> bool:
    identity = _read_run_identity(ctx)
    return identity is not None and consume_cancel(identity.project, identity.run_id)


def _find_blocking_upstream(
    workflow_stage: WorkflowStage, blocked: set[str]
) -> list[str]:
    """Only correct in topological order: every producer must already have been processed."""
    return [
        ref.id for ref in workflow_stage.inputs if ref.id in blocked
    ]


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
