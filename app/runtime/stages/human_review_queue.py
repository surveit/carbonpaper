"""Handler for the human_review_queue stage type.

Every input row yields exactly one output row in its own input position; a row with
no cached decision is marked deferred, never defaulted. On any deferral the mapper
writes a fingerprints sidecar POSITIONALLY aligned to the snapshot's rows, and halts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.predicate import parse_predicate
from app.models import Stage
from app.models.stages.human_review_queue import (
    HumanReviewQueueStage,
    QueueConfig,
    ReviewVerdict,
)
from app.core.stage_cache import compute_row_fingerprint

from ..context import RunContext
from ..manifest import QueueStats, StageContribution
from ..errors import HaltForReview
from .execution import ROW_DEFERRED_KEY, Row, RowMapper, narrow_stage


@dataclass(frozen=True)
class PendingReview:
    """One row awaiting a human decision, carried on the deferred marker of the row that made it."""

    # The key the cache was searched under, and a copy of the row exactly as it
    # arrived from upstream.
    input_fingerprint: str
    frozen_row: Row
    # 0-based position in this stage's INPUT frame — also its position in the
    # upstream stage's output frame, since a row-mapped stage neither reorders
    # nor fans out rows.
    row_ordinal: int


def make_human_review_mapper(stage: Stage, ctx: RunContext, src: pd.DataFrame) -> RowMapper:
    """The callable that decides one row's outcome for one execution of this stage."""
    # The one place every path through this stage passes, so the frame is checked
    # against the declared columns here — before a row is mapped, a snapshot
    # written or a halt raised. Auto-approve is answered here and goes no further:
    # `_approve_row` reaches for no project scope, no cache, no disk and no filter,
    # so a run carrying none of those can still pass a queue stage through.
    queue = narrow_stage(stage, HumanReviewQueueStage).queue
    validate_reviewed_sources_present(queue, src, stage.id)
    if ctx.queue_auto_approve:
        return partial(_approve_row, queue)
    return _QueueRowMapper(stage, queue, ctx, src)


def validate_reviewed_sources_present(
    queue: QueueConfig, src: pd.DataFrame, sid: str
) -> None:
    """Raises when the frame this run produced lacks a column `reviewed_columns` names."""
    # Authoring-time validation checks `reviewed_columns` against the input edge's
    # DECLARED schema; this is the complement. A queued row never reads its source
    # column (the human supplies the value), so without this check a frame missing
    # one would halt for review instead of failing.
    missing = sorted(set(queue.reviewed_columns) - set(src.columns))
    if missing:
        raise ValueError(
            f"human_review_queue '{sid}': queue.reviewed_columns names source column(s) "
            f"{missing}, which this stage's actual input frame does not carry "
            f"(it has {sorted(src.columns)}). The frame does not match the schema the "
            "stage declares — no value may stand in for a missing column."
        )


class _QueueRowMapper:
    """One execution of a queue stage: the per-row outcome and the post-map step
    that reports what the rows produced.

    The one thing a row's outcome depends on beyond the row itself is settled in
    `__init__`, once for the whole stage: the queue filter's verdict for every
    row (one frame-wide evaluation, so a filter that cannot be evaluated fails
    before any row is mapped). A row's outcome is then a lookup by position.

    Nothing is accumulated across rows. The item counts are computed in
    `finish_mapped_rows` from the assembled frame, so a map that raises instead
    (a cancel) reports nothing and leaves whatever the manifest already held:
    for a resumed run, the counts of the halt it is resuming."""

    def __init__(
        self, stage: Stage, queue: QueueConfig, ctx: RunContext, src: pd.DataFrame
    ) -> None:
        self._queue = queue
        _require_project_scope(ctx, stage.id)
        self._queueable = _compute_queueable_mask(src, queue.filter, stage.id)

    def __call__(self, row: Row, index: int) -> Row:
        if not self._queueable[index]:
            return _skip_row(self._queue, row)
        return _defer_row(row, index)

    def finish_mapped_rows(
        self,
        stage: Stage,
        df: pd.DataFrame,
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None:
        """Report the item counts the completed map produced, then write the
        snapshot and sidecar for every row it deferred and raise
        `HaltForReview` — the run cannot go past a row whose value only a human
        can produce. Returns after the counts when no row was deferred. The
        halt carries the same `contribution`, because on that path the raise is
        this stage's only return path into the manifest."""
        contribution.human_review_queue_stats = _compute_queue_stats(self._queue, df)
        pending = _find_pending_reviews(df)
        if not pending:
            return
        queue_path = _write_queue_files(ctx.require_run_dir() / "queue", stage, pending)
        raise HaltForReview(
            stage_id=stage.id,
            pending_count=len(pending),
            queue_path=queue_path,
            contribution=contribution,
        )


# --- _QueueRowMapper.__init__: once per stage execution ------------------------


def _require_project_scope(ctx: RunContext, sid: str) -> None:
    """Raise unless the run grants project scope (`ctx.identity` and
    `ctx.stage_cache`). Without it no decision can ever be replayed, so every
    queueable row would defer and the run could never get past this stage: a
    subset/preview run's context must fail loudly here rather than be silently
    let through."""
    if ctx.identity is None or ctx.stage_cache is None:
        raise ValueError(
            f"human_review_queue '{sid}' requires a project-scoped (production) "
            "run: RunContext.identity and RunContext.stage_cache must both be "
            "set, but this run carries neither."
        )


def _compute_queueable_mask(src: pd.DataFrame, flt: str | None, sid: str) -> list[bool]:
    """Whether each row of `src` is subject to review, one verdict per row in
    `src`'s own row order. With no filter every row is; with one, the filter is
    evaluated ONCE over the whole frame and the verdicts read off positionally.

    A `PredicateError` from the parse means `flt` falls outside the closed
    grammar `parse_predicate` enforces (bad grammar) and propagates unwrapped,
    so such a filter fails at parse rather than being misreported as an
    evaluation failure. A failure while EVALUATING — `flt` parses but references
    a column absent from this run's actual frame, or a cell the expression
    cannot answer for — becomes a ValueError naming this stage and the filter
    text: a malformed reference must halt the run rather than silently routing
    every row to passthrough, which would skip human review unnoticed."""
    if not flt:
        return [True] * len(src)
    parsed = parse_predicate(flt)
    try:
        # eval of a comparison yields a bool Series; the explicit dtype=bool
        # conversion makes that a checked fact (anything else lands in the
        # except below and is raised as a loud error).
        mask = pd.Series(src.eval(parsed.pandas_expr), index=src.index, dtype=bool)
    except (SyntaxError, ValueError, TypeError, KeyError, AttributeError, NameError) as exc:
        raise ValueError(
            f"human_review_queue '{sid}' filter could not be evaluated: `{flt}` "
            f"({type(exc).__name__}: {exc}). A filter must reference existing input columns."
        ) from exc
    return [bool(verdict) for verdict in mask]


# --- the row outcomes the mapper does not need its own state for ---------------


def _defer_row(row: Row, index: int) -> Row:
    """A queueable row nobody has decided: a deferred marker and nothing else."""
    # Never a substituted, defaulted or partially-filled row — the value does not
    # exist yet. The fingerprint is the key the driver's row cache looked this row
    # up under, so the decision recorded against it resolves on the next run.
    return {
        ROW_DEFERRED_KEY: PendingReview(
            input_fingerprint=compute_row_fingerprint(row),
            frozen_row=dict(row),
            row_ordinal=index,
        )
    }


def _skip_row(queue: QueueConfig, row: Row) -> Row:
    """A row the filter did not select: the upstream values stand, verdict `skipped`."""
    # Declaring a filter is the author's statement that the upstream values stand
    # for the rows it excludes, so those values are copied into the reviewed
    # columns — but the verdict is `skipped`, not `approve`: nobody looked at it.
    return _add_review_columns(queue, row, ReviewVerdict.skipped)


def _approve_row(queue: QueueConfig, row: Row, index: int) -> Row:
    """Writes `approve` although nobody looked, which `_skip_row` refuses to do."""
    # This path exists only under `queue_auto_approve`, which RunContext keeps off
    # every run whose output is a real artifact. The outcome depends on the row
    # alone, so its position in the input is not read.
    return _add_review_columns(queue, row, ReviewVerdict.approve)


def _add_review_columns(queue: QueueConfig, row: Row, verdict: ReviewVerdict) -> Row:
    added: Row = {
        target: row[source] for source, target in queue.reviewed_columns.items()
    }
    added[queue.verdict_column] = verdict.value
    added[queue.reviewer_column] = pd.NA
    added[queue.reviewed_at_column] = pd.NA
    if queue.review_notes_column is not None:
        added[queue.review_notes_column] = pd.NA
    return {**row, **added}


# --- finish_mapped_rows: the deferred rows, the snapshot and its sidecar ------


def _compute_queue_stats(queue: QueueConfig, df: pd.DataFrame) -> QueueStats:
    """The stage's item counts, read off the assembled frame rather than accumulated."""
    # A row the driver's cache served never reaches the mapper, and every count is a
    # property of the row it produced. Each row's declared verdict column shows which
    # outcome it took — a deferred marker for a pending row, `skipped` for one the
    # filter passed through, any other verdict for one a human decided. Queued total
    # is the queueable rows: pending plus decided.
    column = queue.verdict_column
    verdicts = list(df[column]) if column in df.columns else []
    passed_through = sum(1 for value in verdicts if value == ReviewVerdict.skipped)
    decided = sum(
        1 for value in verdicts
        if not pd.isna(value) and value != ReviewVerdict.skipped
    )
    pending = len(_find_pending_reviews(df))
    return {
        "items_queued_total": pending + decided,
        "items_passed_through": passed_through,
        "items_pending": pending,
        "items_decided": decided,
    }


def _find_pending_reviews(df: pd.DataFrame) -> list[PendingReview]:
    """Every `PendingReview` the map attached to a deferred row, in the
    assembled frame's own row order. A frame with no deferred column at all
    means no row was deferred — every one of them was passed through or
    decided."""
    if ROW_DEFERRED_KEY not in df.columns:
        return []
    return [value for value in df[ROW_DEFERRED_KEY] if isinstance(value, PendingReview)]


def _write_queue_files(
    queue_dir: Path, stage: Stage, pending: list[PendingReview]
) -> Path:
    """Write the snapshot and its fingerprint sidecar into `queue_dir`,
    creating the directory, and return the snapshot's path."""
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = _write_pending_snapshot(queue_dir, stage.id, pending)
    _write_fingerprint_sidecar(
        queue_dir, stage.id, stage.compute_definition_fingerprint(), pending
    )
    return queue_path


def _write_pending_snapshot(queue_dir: Path, sid: str, pending: list[PendingReview]) -> Path:
    """Persist the pending rows for the reviewer UI and return the path
    written. The frame is built from the frozen rows themselves, so it holds
    exactly their original upstream columns — there is no step at which a
    fingerprint or decision column could be added to it."""
    frame = pd.DataFrame([item.frozen_row for item in pending])
    queue_path = queue_dir / f"{sid}.parquet"
    try:
        frame.to_parquet(queue_path, index=False)
    except (pa_lib.ArrowException, ValueError, TypeError):
        # A column whose dtype/shape parquet can't represent (mixed-type
        # object columns, nested Python values) — CSV stringifies those and
        # succeeds. A disk/OS error (ENOSPC, permission) is deliberately NOT
        # caught here: it would fail identically for CSV, so it propagates
        # (and is recorded by the runner) rather than silently degrading the
        # queue snapshot.
        queue_path = queue_dir / f"{sid}.csv"
        frame.to_csv(queue_path, index=False)
    return queue_path


def _write_fingerprint_sidecar(
    queue_dir: Path, sid: str, stage_fingerprint: str, pending: list[PendingReview]
) -> None:
    """`input_fingerprints` and `row_ordinals` are POSITIONALLY aligned to the snapshot."""
    # Both are in the pending rows' own order; `stage_fingerprint` is the one every
    # pending row of this halt shares. `row_ordinals` are positions in this stage's
    # INPUT frame, so they are NOT 0..n-1 whenever a queue filter passed some rows
    # through unreviewed.
    (queue_dir / f"{sid}.fingerprints.json").write_text(
        json.dumps({
            "stage_fingerprint": stage_fingerprint,
            "input_fingerprints": [item.input_fingerprint for item in pending],
            "row_ordinals": [item.row_ordinal for item in pending],
        }),
        encoding="utf-8",
    )
