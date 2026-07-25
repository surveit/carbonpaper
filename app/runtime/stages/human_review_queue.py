"""Handler for the human_review_queue stage type.

The per-row compute this stage performs is "ask a human". It has one answer it
can produce synchronously — a decision a human already recorded for this exact
(stage definition, input row) pair, replayed from the stage-result cache — and
one it cannot: for a row nobody has decided yet, the answer does not exist, and
no default may stand in for it.

So each row ends in exactly one of four outcomes:

  - the queue filter did not match it → it passes through, carrying its AI
    score as final and the pass-through reviewer columns;
  - a cached decision holds an output row → that row, replayed verbatim. What
    the payload MEANS is built and interpreted above this seam; this module
    neither constructs nor reads it;
  - a cached decision holds a tombstone → the row is marked for removal
    (`ROW_DROP_KEY`), because the human rejected it;
  - no cached decision → the row is marked deferred (`ROW_DEFERRED_KEY`)
    carrying the fingerprint it was looked up under and a frozen copy of
    itself, and nothing else.

The mapper's own `finish_mapped_rows` runs after the map: it reports the item
counts the map accumulated onto the stage's `StageContribution`, then reads
those deferred markers back. Where a row was deferred it writes the two files
the reviewer UI reads and raises `HaltForReview`:

  - `<run_dir>/queue/<stage>.parquet` (or `.csv` when a dtype defeats parquet),
    the snapshot — built from the frozen rows themselves, so it holds exactly
    the original upstream columns and no bookkeeping of any kind;
  - `<run_dir>/queue/<stage>.fingerprints.json`, the sidecar, holding the one
    `stage_fingerprint` this halt shares and `input_fingerprints` POSITIONALLY
    aligned to the snapshot's rows. A fingerprint is never row data, so it
    lives only here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.errors import PredicateError
from app.core.predicate import evaluate_predicate, parse_predicate
from app.models import RowReviewDecision, Stage
from app.core.stage_cache import ReadOnlyStageCache, StageCacheEntry, compute_row_fingerprint

from ..context import RunContext
from ..manifest import QueueStats, StageContribution
from ..errors import HaltForReview
from .execution import ROW_DEFERRED_KEY, ROW_DROP_KEY, Row

# The upstream AI score column a queue stage reviews. Named once so the two sites
# that test for its presence (auto-approve and passthrough finalization) can't
# drift apart.
_SCORE_COLUMN = "score"


@dataclass(frozen=True)
class PendingReview:
    """One row awaiting a human decision: the `input_fingerprint` the cache was
    searched under, and `frozen_row`, a copy of the row exactly as it arrived
    from upstream. Carried on the deferred marker of the row that produced it,
    which is the only place either value exists until the snapshot and its
    sidecar are written."""

    input_fingerprint: str
    frozen_row: Row


def make_human_review_mapper(stage: Stage, ctx: RunContext) -> Callable[[Row], Row]:
    """The callable that decides one row's outcome for one execution of this
    stage.

    Auto-approve is answered here and goes no further: `_approve_row` reaches
    for no project scope, no cache and no disk, so a run that carries none can
    still pass a queue stage through."""
    if ctx.queue_auto_approve:
        return _approve_row
    return _QueueRowMapper(stage, ctx)


class _QueueRowMapper:
    """One execution of a queue stage: the per-row decision and the post-map
    step that reports what the rows produced, holding between them the state
    both need.

    The cached decisions are read in `__init__`, ONCE for the whole stage: a
    row's outcome is then a dictionary lookup, never a store read. The item
    counters live here too and are incremented as rows are mapped, which is the
    only point at which a row the driver later removes (a tombstoned one) is
    still countable. They reach the stage's `StageContribution` — what the
    executor folds into the run manifest — only in `finish_mapped_rows`, so a
    map that raises instead (a cancel, a filter that cannot be evaluated)
    reports nothing and leaves whatever the manifest already held: for a
    resumed run, the counts of the halt it is resuming."""

    def __init__(self, stage: Stage, ctx: RunContext) -> None:
        assert stage.queue is not None  # Stage validation: human_review_queue carries queue
        self._entries = _read_cached_decisions(stage, ctx)
        self._is_queueable = _make_queueable_test(stage.id, stage.queue.filter)
        self._stats: QueueStats = {
            "items_queued_total": 0, "items_passed_through": 0,
            "items_pending": 0, "items_decided": 0,
        }

    def __call__(self, row: Row) -> Row:
        if not self._is_queueable(row):
            self._stats["items_passed_through"] += 1
            return _pass_row_through(row)
        self._stats["items_queued_total"] += 1
        return self._replay_decision_or_defer(row)

    def finish_mapped_rows(
        self,
        stage: Stage,
        df: pd.DataFrame,
        ctx: RunContext,
        contribution: StageContribution,
    ) -> None:
        """Report the item counts the completed map accumulated, then write the
        snapshot and sidecar for every row it deferred and raise
        `HaltForReview` — the run cannot go past a row whose value only a human
        can produce. Returns after the counts when no row was deferred. The
        halt carries the same `contribution`, because on that path the raise is
        this stage's only return path into the manifest."""
        contribution.human_review_queue_stats = self._stats
        pending = _find_pending_reviews(df)
        if not pending:
            return
        queue_dir = ctx.require_run_dir() / "queue"
        queue_dir.mkdir(parents=True, exist_ok=True)
        queue_path = _write_pending_snapshot(queue_dir, stage.id, pending)
        _write_fingerprint_sidecar(
            queue_dir, stage.id, stage.compute_definition_fingerprint(), pending
        )
        raise HaltForReview(
            stage_id=stage.id,
            pending_count=len(pending),
            queue_path=queue_path,
            contribution=contribution,
        )

    def _replay_decision_or_defer(self, row: Row) -> Row:
        """A row subject to review, resolved against the decisions already
        recorded: the cached output row replayed as-is, a drop marker where the
        cached output is a tombstone, or — with no cached decision — a deferred
        marker carrying the row's fingerprint and a frozen copy of it, and
        nothing else. Never a substituted, defaulted or partially-filled row:
        the value does not exist yet."""
        fingerprint = compute_row_fingerprint(row)
        entry = self._entries.get(fingerprint)
        if entry is None:
            self._stats["items_pending"] += 1
            return {
                ROW_DEFERRED_KEY: PendingReview(
                    input_fingerprint=fingerprint, frozen_row=dict(row)
                )
            }
        self._stats["items_decided"] += 1
        if entry.output_row is None:
            # A plain Python bool: the driver removes a row on exactly `is True`.
            return {ROW_DROP_KEY: True}
        return dict(entry.output_row)


# --- _QueueRowMapper.__init__: once per stage execution ------------------------


def _read_cached_decisions(stage: Stage, ctx: RunContext) -> dict[str, StageCacheEntry]:
    """Every decision already recorded against this exact stage definition,
    keyed by the input fingerprint it was filed under. This is the stage's one
    and only cache read: a row's outcome is then a dictionary lookup, and a
    queue stage's store cost does not scale with its row count."""
    project, stage_cache = _require_project_scope(ctx, stage.id)
    return {
        entry.input_fingerprint: entry
        for entry in stage_cache.find_entries(
            project, stage.id, stage.compute_definition_fingerprint()
        )
    }


def _require_project_scope(ctx: RunContext, sid: str) -> tuple[str, ReadOnlyStageCache]:
    """The (project, cache) pair a queue stage needs to look up cached
    decisions: `ctx.identity.project` and `ctx.stage_cache`, typed down to
    `ReadOnlyStageCache` — this handler only ever reads, so mypy proves it
    never calls a write method (`ReadOnlyStageCache` has none). Raises loudly
    if either is absent: a human_review_queue stage always runs inside a
    project-scoped (production) run; a subset/preview run's context (which
    carries neither) cannot resolve a cache key and must not be silently let
    through."""
    if ctx.identity is None or ctx.stage_cache is None:
        raise ValueError(
            f"human_review_queue '{sid}' requires a project-scoped (production) "
            "run: RunContext.identity and RunContext.stage_cache must both be "
            "set, but this run carries neither."
        )
    return ctx.identity.project, ctx.stage_cache


def _make_queueable_test(sid: str, flt: str | None) -> Callable[[Row], bool]:
    """Whether one row is subject to review. With no filter, every row is; with
    one, the row's own verdict for it.

    The filter is parsed once, here — a `PredicateError` from the parse means
    `flt` falls outside the closed grammar (bad grammar) and propagates
    unwrapped, so such a filter fails at parse rather than being misreported as
    an evaluation failure. A `PredicateError` from EVALUATING a row (a
    referenced column the row does not carry, a cell the expression cannot
    answer for) becomes a loud ValueError instead: a malformed reference must
    halt the run rather than silently routing every row to passthrough, which
    would skip human review unnoticed."""
    if not flt:
        return lambda row: True
    parsed = parse_predicate(flt)

    def is_queueable(row: Row) -> bool:
        try:
            return evaluate_predicate(parsed, row)
        except PredicateError as exc:
            raise ValueError(
                f"human_review_queue '{sid}' filter could not be evaluated: `{flt}` "
                f"({type(exc).__name__}: {exc}). A filter must reference existing input columns."
            ) from exc

    return is_queueable


# --- the row outcomes the mapper does not need its own state for ---------------


def _pass_row_through(row: Row) -> Row:
    """A row the filter did not select: its AI score stands as final, and the
    reviewer columns carry the pass-through values (no human saw this row)."""
    passed: Row = dict(row)
    if _SCORE_COLUMN in row:
        passed["ai_score"] = row[_SCORE_COLUMN]
        passed["final_score"] = row[_SCORE_COLUMN]
    passed["human_score"] = pd.NA
    passed["reviewer_id"] = row.get("reviewer", pd.NA)
    passed["reviewed_at"] = pd.NA
    passed["review_notes"] = "below review threshold"
    return passed


def _approve_row(row: Row) -> Row:
    """Approve one row in memory, as `ctx.queue_auto_approve` asks: the same
    reviewer columns an `approve` decision produces (final and human score =
    the AI score), with reviewer_id/reviewed_at null because no human reviewed
    it. Reads no cache and writes no file."""
    ai = row[_SCORE_COLUMN] if _SCORE_COLUMN in row else pd.NA
    return {
        **row,
        "ai_score": ai,
        "human_score": ai,
        "final_score": ai,
        "review_notes": f"decision={RowReviewDecision.approve}",
        "reviewer_id": pd.NA,
        "reviewed_at": pd.NA,
        "decision": RowReviewDecision.approve,
    }


# --- finish_mapped_rows: the deferred rows, the snapshot and its sidecar ------


def _find_pending_reviews(df: pd.DataFrame) -> list[PendingReview]:
    """Every `PendingReview` the map attached to a deferred row, in the
    assembled frame's own row order. A frame with no deferred column at all
    means no row was deferred — every one of them was passed through or
    decided."""
    if ROW_DEFERRED_KEY not in df.columns:
        return []
    return [value for value in df[ROW_DEFERRED_KEY] if isinstance(value, PendingReview)]


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
    """Write `<stage>.fingerprints.json`: the one `stage_fingerprint` every
    pending row of this halt shares, and `input_fingerprints` in the pending
    rows' own order — POSITIONALLY aligned to the snapshot written from the
    same list."""
    (queue_dir / f"{sid}.fingerprints.json").write_text(
        json.dumps({
            "stage_fingerprint": stage_fingerprint,
            "input_fingerprints": [item.input_fingerprint for item in pending],
        }),
        encoding="utf-8",
    )
