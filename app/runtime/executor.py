"""Shared stage-execution engine, driven both by the production run lifecycle
(`app/runtime/runner.py`) and by `run_subset` here, the non-production subset
executor used by evals and previews. This module never creates a production
run record; that split is what lets an import-linter contract keep evals away
from the production run entry points."""

from __future__ import annotations

import enum
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Iterable, NamedTuple

import pandas as pd

from app.core.errors import SubsetRunError
from app.core.frame_checks import find_duplicate_row_violations
from app.core.frames import write_frame_file, write_frame_file_with_csv_fallback
from app.models import Stage, StageType, Workflow
from app.models.run_manifest import (
    RowError,
    RunManifest,
    SCHEMA_REFUSAL_ERROR_TYPE,
    StageContribution,
    StageErrorInfo,
    StageRecord,
)
from app.models.run_parameters import RunParameters
from app.core.run_status import RunStatus, StageStatus

from .cancellation import consume_cancel
from .context import RunContext, RunIdentity
from .errors import RunCancelled
from .manifest import CONTRIBUTION_ATTR, create_run_manifest, write_manifest
from .run_log import RUN_START, STAGE_DONE, STAGE_START, RunLog
from .stages import HANDLERS, HaltForReview, StageHandler
from .lineage import (
    LINEAGE_ATTR,
    RowLineage,
    concatenated_inputs_lineage,
    kept_rows_lineage,
    lineage_sidecar_path,
    read_row_lineage,
)
from app.models.severity import UserFacingErrorSeverity
from .validation import Issue, ValidationReport, validate_dataframe


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
    params: RunParameters = RunParameters(),
    project: str | None = None,
    workflow_version: str | None = None,
    identity: RunIdentity | None = None,
) -> dict[str, pd.DataFrame]:
    """Run only `stage_ids` of `workflow`, with `injected_outputs` seeded as the
    outputs of stages OUTSIDE the subset (their upstream is cut off — the output is
    given, not computed). Returns the outputs of every executed stage.

    Owns its run manifest as a first-class, live-updated artifact: it mints the
    manifest here (create_run_manifest, the single source of the manifest shape)
    and drives it through the same `_execute_stages` engine a production run uses,
    which flushes the manifest to disk per stage. So if a mid-frontier stage
    errors, the manifest on disk already records the completed stages as ok and the
    failing stage's error before this raises — partial work is preserved for a
    caller to read back, not lost to a save-at-the-end.

    `project`/`workflow_version` are the run's logical identity, recorded in the
    manifest when a caller knows them and left None otherwise (never fabricated).
    The run_id is `run_dir.name`.

    Any input of a subset stage that names a stage outside the subset must appear in
    `injected_outputs`, or `_execute_stages` fails on it. Raises SubsetRunError if an
    executed stage errors or the run halts for review, so a caller gets a clean output
    set or a loud failure — never a half-populated dict.

    `queue_auto_approve` seeds the ctx flag of the same name: when set, a
    human_review_queue stage passes every row through in memory (approving all,
    no disk) instead of reaching for the decisions store. Off by default, so an
    ordinary subset run's queue stage behaves exactly as before.

    `identity` makes this a workflow test's run — project scope plus a read-only
    stage-result cache (`RunContext.for_workflow_test_run`); a workflow test is
    the only current source of one. None (the default) is the plain subset run
    (`RunContext.for_stages_outside_a_run`): no identity, no cache access,
    `trace_links` unavailable to a publish stage.
    `is_test_run` is recorded on the manifest (`RunManifest.is_test_run`);
    default False, so an ordinary subset run's manifest reads as a real run.

    `limits`/`offsets` are the per-stage row window a production run takes them as
    (prepare_run) — for a stage with no inputs, the window is taken on the frame it
    loads, so a source stage inside the subset reads the same rows an injected
    slice would have given it. Recorded on the manifest, and every cut taken is
    noted on the stage record."""
    by_id = workflow.index_stages_by_id()
    missing = [sid for sid in stage_ids if sid not in by_id]
    if missing:
        raise SubsetRunError(f"subset names stage(s) not in the workflow: {missing}")
    ordered = topological_sort([by_id[sid] for sid in stage_ids])
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    ctx = _subset_ctx(repo_root, run_dir, identity, params)
    manifest = create_run_manifest(
        ordered, ctx, run_id=run_dir.name, project=project,
        workflow_version=workflow_version, input_bindings={})
    write_manifest(run_dir, manifest)
    outputs: dict[str, pd.DataFrame] = dict(injected_outputs)
    manifest = _execute_stages(ordered, ctx, manifest, run_dir, outputs)
    _raise_if_run_failed(manifest)
    return outputs


def _subset_ctx(
    repo_root: Path, run_dir: Path, identity: RunIdentity | None, params: RunParameters,
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
            repo_root, run_dir, identity.project, identity.run_id, params)
    return RunContext.for_stages_outside_a_run(repo_root, run_dir, params)


def _raise_if_run_failed(manifest: RunManifest) -> None:
    """Turn a non-clean manifest into a SubsetRunError naming the cause. Reads the
    same status/stage records `_execute_stages` writes — the manifest is the run's
    result of record, so failure detection lives with it, not in each caller."""
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
    """Execute ordered stages under this run's own event log."""
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
    """What `_run_stage` learned about the stage it just ran, for the loop to
    act on. `RAN` covers `ok`, `validation_warnings`, and `error` alike —
    none of those need anything beyond letting the loop move to the next
    stage. `HALTED` additionally needs the loop to remember this stage id for
    `halted_at`. `CANCELLED` needs the loop to stop."""

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
    """Write the manifest mid-run so the run page can show live progress (stages
    light up as they start/finish) instead of the whole pipeline running silently
    and updating only at the very end. Persists a copy stamped with `updated_at`
    and the given `status` (and the current per-stage records) WITHOUT mutating
    the live manifest — so the final finalize-time write is not left carrying a
    mid-run `updated_at` or a `running` status. The accumulated
    human_review_queue_stats/dropped_columns already live on `manifest` (merged
    per stage by
    _merge_stage_contribution)."""
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
    stage: Stage, outputs_so_far: dict[str, pd.DataFrame], ctx: RunContext,
    record: StageRecord,
) -> tuple[dict[str, pd.DataFrame], _RowWindow]:
    """The rows this stage's handler will be given, keyed by producer id, cut to
    this run's row window BEFORE the duplicate-row and input-schema checks run —
    so a `limit: 3` dry run is never failed by row 4,000, which it never reads.
    Raises if an upstream output is missing — the caller's exception handling
    turns that into this stage's own error."""
    sid = stage.id
    window = _resolve_row_window(stage, ctx)
    inputs_for_stage: dict[str, pd.DataFrame] = {}
    for ref in stage.inputs:
        if ref.id not in outputs_so_far:
            raise RuntimeError(f"Upstream stage '{ref.id}' has no output yet")
        df = _take_row_window(
            outputs_so_far[ref.id], window, f"from input '{ref.id}'", record)
        _reject_duplicate_input_rows(df, ref.id, sid)
        inputs_for_stage[ref.id] = df
        if ref.table_schema is not None:
            rep = validate_dataframe(
                df, ref.table_schema, stage_id=sid, phase=f"input:{ref.id}",
            )
            record.input_validation_report.append(rep.to_dict())
    return inputs_for_stage, window


def _resolve_handler(stage_type: StageType) -> StageHandler:
    handler = HANDLERS.get(stage_type)
    if handler is None:
        raise ValueError(f"No handler for stage type '{stage_type}'")
    return handler


def _record_halt(record: StageRecord, halt: HaltForReview, run_dir: Path) -> None:
    """Fork-blocking, not loop-ending: this stage awaits review and blocks
    its downstream, while independent forks keep running."""
    record.status = StageStatus.AWAITING_REVIEW
    record.output_row_count = halt.pending_count
    # Manifest paths are POSIX-style so the persisted JSON is identical on
    # every platform.
    record.queue_path = halt.queue_path.relative_to(run_dir).as_posix()


def _record_stage_error(record: StageRecord, exc: Exception) -> None:
    """Record any stage failure (a handler can raise ValueError, RuntimeError,
    a pandas/pyarrow error, etc.) in the manifest. This outcome always joins
    the caller's `blocked` set, so its transitive downstream is skipped and
    never marked `ok` on this stage's absent output."""
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


def _resolve_row_window(stage: Stage, ctx: RunContext) -> _RowWindow:
    """The window this run reads: --offset stage=M, then --limit stage=N over the stage's `limit:`."""
    offset = ctx.params.offsets.get(stage.id)
    cap = ctx.params.limits.get(stage.id, stage.limit)
    return _RowWindow(
        offset if isinstance(offset, int) and offset > 0 else 0,
        cap if isinstance(cap, int) and cap >= 0 else None,
    )


def _take_row_window(
    df: pd.DataFrame, window: _RowWindow, source: str, record: StageRecord
) -> pd.DataFrame:
    """`window` of `df`'s rows; every cut actually taken is noted on `record`, never silent."""
    if window.start > 0 and len(df) > 0:
        record.add_note(
            f"offset={window.start}: skipped the first "
            f"{min(window.start, len(df))} of {len(df)} row(s) {source}"
        )
        df = df.iloc[window.start:].reset_index(drop=True).copy()
    if window.cap is not None and len(df) > window.cap:
        record.add_note(f"limit={window.cap}: read {window.cap} of {len(df)} row(s) {source}")
        df = df.head(window.cap).copy()
    return df


def _persist_row_lineage(lineage: RowLineage, sid: str, run_dir: Path) -> None:
    """Write `sid`'s per-row provenance sidecar (source stage id + row ordinal,
    one row per this stage's own output row, in output order) that
    `app.runtime.trace` reads to cross a hop that isn't row-preserving by
    position alone (filter_rows, union)."""
    write_frame_file(lineage.to_frame(), lineage_sidecar_path(run_dir, sid))


def _stage_row_lineage(
    stage: Stage, output: pd.DataFrame | None, inputs: dict[str, pd.DataFrame],
    window: _RowWindow,
) -> RowLineage | None:
    """This stage's per-row provenance, or None where output row i is input row
    i and the trace needs no help crossing it.

    Every source is the runtime's own knowledge, never the stage's report of
    itself: the row driver's record of which input ordinals it emitted, riding
    the frame's `.attrs`; for a union, the row counts of the inputs the runtime
    handed over, since concatenation is in declared order; and, for a stage the
    runtime sliced, the window it cut. The first two count from the start of the
    SLICED input frames the handler was given, so both are shifted back onto the
    upstream stage's own ordinals."""
    driven = read_row_lineage(output)
    if driven is not None:
        return driven.shifted(window.start)
    if stage.type == StageType.union:
        return concatenated_inputs_lineage(stage, inputs, window.start)
    return _sliced_input_lineage(stage, output, window)


def _sliced_input_lineage(
    stage: Stage, output: pd.DataFrame | None, window: _RowWindow
) -> RowLineage | None:
    """A sliced stage's rows named by their true upstream ordinals, for the trace to cross."""
    if window.start == 0 and window.cap is None:
        return None
    if not stage.is_grain_and_order_preserving or len(stage.inputs) != 1:
        return None
    rows = 0 if output is None else len(output)
    return kept_rows_lineage(
        stage.inputs[0].id, list(range(window.start, window.start + rows)))


def _persist_stage_output(output: pd.DataFrame, sid: str, run_dir: Path, record: StageRecord) -> Path:
    """The stage's artifact path, after writing it — the CSV fallback is NOTED on `record`."""
    written = write_frame_file_with_csv_fallback(
        output, run_dir / "outputs" / f"{sid}.parquet"
    )
    if written.parquet_error is not None:
        record.add_note(f"Wrote CSV instead of parquet: {written.parquet_error}")
    return written.path


def _finalize_stage_output(
    stage: Stage,
    window: _RowWindow,
    record: StageRecord,
    output: pd.DataFrame | None,
    inputs_for_stage: dict[str, pd.DataFrame],
    outputs_so_far: dict[str, pd.DataFrame],
    run_dir: Path,
    manifest: RunManifest,
) -> bool:
    """Validate and persist a stage's raw handler output, then decide
    its terminal status. Two things make it a stage error, both recorded
    exactly like a raised exception: a per-row generation failure, and an
    error-severity issue in the OUTPUT validation report (a missing declared
    column, a failed coercion, a value outside a declared enum, a null in a
    non-nullable column, a duplicated primary key) — a frame that violates the
    declared schema must not be consumed downstream. The output file stays on
    disk for inspection, and the stage's own `error` status keeps a resume from
    reusing it. Otherwise the status is `ok`, or `validation_warnings` when an
    INPUT report carries an error. Returns True if the caller must join this
    stage to `blocked`, so every transitive consumer is skipped rather than run
    on this stage's non-conforming frame and marked `ok`; False otherwise."""
    sid = stage.id
    # Read the stage's contribution off the handler's frame BEFORE any frame is
    # rebuilt (which drops `.attrs`), then merge usage into this record and
    # human_review_queue_stats/dropped_columns into the manifest.
    contribution = _read_stage_contribution(output)
    row_errors = _merge_stage_contribution(contribution, sid, manifest, record)

    if output is None:
        output = pd.DataFrame()
    # Drop the contribution channel so it never reaches the persisted parquet
    # (its metadata isn't JSON-serializable) — it has been merged above.
    output.attrs.pop(CONTRIBUTION_ATTR, None)
    lineage = _stage_row_lineage(stage, output, inputs_for_stage, window)
    # Drop the lineage channel for the same reason as the contribution one
    # above: `.attrs` is not JSON-serializable and must not reach the parquet.
    output.attrs.pop(LINEAGE_ATTR, None)
    if not stage.inputs:
        # A stage with no inputs originates its rows outside the run, so the
        # frame it just loaded is the runtime's only handle on them: its window
        # is taken here rather than on an input frame that does not exist.
        output = _take_row_window(output, window, "loaded from the source", record)
    if lineage is not None:
        _persist_row_lineage(lineage, sid, run_dir)

    out_rep = validate_dataframe(output, stage.resolve_output_schema(), stage_id=sid, phase="output")
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
            type=SCHEMA_REFUSAL_ERROR_TYPE,
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
    """The StageContribution a handler attached to its output frame's `.attrs`,
    or an empty one when there is no frame (a stage that produced nothing) or no
    contribution (a handler that reported none)."""
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
    """Merge one stage's contribution into the run's living record: its token
    usage and run notes onto the stage record, its dropped-column and
    human-review-queue tallies onto the
    manifest's per-stage maps. Returns the row-generation errors for the caller
    to fold into the output validation report and terminal status."""
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
    """Run one stage end to end: gather its inputs, invoke its handler,
    process and persist its output, and record the outcome (ok, warnings,
    error, halt, or a mid-stage cancel) into `records_by_id[stage.id]` —
    flushing the manifest once the stage starts and again once it settles, so
    the run page shows it live. Returns `(outcome, joins_blocked)`:
    `joins_blocked` is True for a halt, a general exception, a row-generation
    error, and an output frame that violates the stage's output schema alike —
    every outcome except a clean ok/warnings or a cancel — so the caller can add this stage to its own `blocked` set
    itself, keeping that decision visible at the loop."""
    sid = stage.id
    record = StageRecord.record_with_status(stage, StageStatus.RUNNING)
    t0 = time.perf_counter()
    records_by_id[sid] = record
    _flush_manifest(manifest, records_by_id, ordered, run_dir, RunStatus.RUNNING)
    _emit_stage_start(ctx.run_log, stage)

    joins_blocked = False
    try:
        inputs_for_stage, window = _gather_stage_inputs(stage, outputs_so_far, ctx, record)
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
            stage, window, record, output, inputs_for_stage, outputs_so_far, run_dir,
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
        log.emit({"kind": STAGE_START, "stage": stage.id, "type": stage.type})


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
    """Assemble and persist the run's final manifest once the loop has
    stopped, in topological order (blocked downstream stages were already
    marked `pending` inline, so no post-loop fill is needed). A cancel is a
    hard stop: it keeps the cancelled outcome regardless of any error/halt a
    stage recorded before the cancel arrived, and carries no `halted_at`, so
    a cancelled run never shows the review banner for a halt that happened
    earlier in the same run."""
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
    """One-line summary of per-row generation failures for the stage's error
    record — the per-row detail lives in the output validation report's issues."""
    head = "; ".join(f"row {e['row']}: {e['message']}" for e in row_errors[:3])
    more = f" (+{len(row_errors) - 3} more)" if len(row_errors) > 3 else ""
    return f"{len(row_errors)} row(s) failed generation: {head}{more}"


def _summarize_output_schema_errors(sid: str, report: ValidationReport) -> str:
    """One-line summary of the error-severity output issues for the stage's
    error record, naming the columns at fault — the full issue list stays in the
    output validation report."""
    errors = [issue for issue in report.issues if issue.severity == UserFacingErrorSeverity.error]
    named = sorted({issue.column for issue in errors if issue.column})
    columns = f" (column(s): {', '.join(named)})" if named else ""
    return (
        f"stage '{sid}' output violates its output schema{columns}: "
        + "; ".join(issue.message for issue in errors)
    )


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
    return record is not None and record.status in (StageStatus.OK, StageStatus.VALIDATION_WARNINGS)


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


def _reject_duplicate_input_rows(df: pd.DataFrame, input_id: str, stage_id: str) -> None:
    """Fail the stage if an input frame carries exact duplicate rows."""
    violations = find_duplicate_row_violations(df)
    if not violations:
        return
    raise ValueError(
        f"Input '{input_id}' to stage '{stage_id}' contains {violations[0].message}"
    )
