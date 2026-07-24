"""Handler for the human_review_queue stage type."""

from __future__ import annotations

import json
from typing import NoReturn

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.predicate import parse_predicate
from app.models import RowReviewDecision, Stage
from app.core.stage_cache import ReadOnlyStageCache, StageCacheEntry, compute_row_fingerprint

from ..context import ACCUMULATION_ATTR, QueueStats, RunContext, StageAccumulation
from ..errors import HaltForReview

# The upstream AI score column a queue stage reviews. Named once so the two sites
# that test for its presence (auto-approve and passthrough finalization) can't
# drift apart.
_SCORE_COLUMN = "score"


def handle_human_review_queue(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Real review-queue semantics:

    1. Apply the queue filter to upstream output → items needing review.
    2. Fingerprint the stage definition once (`stage_fp`) and every queueable
       row (`input_fingerprints_by_index`, over its original upstream
       columns) — index-aligned, never burned onto the row itself.
    3. Look up this stage definition's cached decisions from `ctx.stage_cache`
       and split queueable rows by whether their fingerprint matches one:
         - items with a cached decision get it applied
         - items without are written to runs/<id>/queue/<stage>.parquet, a
           PURE snapshot of their original upstream columns, alongside a
           `<stage>.fingerprints.json` sidecar naming the fingerprints.
    4. If ANY items lack decisions, raise HaltForReview so the runner can
       stop downstream execution and mark the run awaiting_review.
    5. Otherwise replace each decided row with its cached output row, dropping
       any whose cached output is a tombstone (the row was dropped upstream of
       this seam), and concat with the passthrough rows. The cached output is
       built and interpreted above this seam; this handler only replays it.
    """
    sid = stage.id
    queue_cfg = stage.queue
    assert queue_cfg is not None  # Stage validation: human_review_queue carries queue_cfg
    src = inputs[stage.inputs[0].id].copy()
    accumulation = StageAccumulation()

    # Checked FIRST, before any reach for project scope / the decisions cache: when
    # the caller asked for in-memory auto-approval, every row is approved and returned
    # without a stage-cache decision lookup, queue snapshot, or halt. A non-production
    # (subset/preview) run carries no project scope, so this is also the only way such
    # a run can pass a queue stage. Production ctx never sets the flag, so production
    # behavior is unchanged.
    if ctx.queue_auto_approve:
        return _auto_approve_all(src, stage, accumulation)

    project, stage_cache = _require_project_scope(ctx, sid)

    queueable, passthrough = _partition_reviewable_rows(src, queue_cfg.filter, sid)

    stage_fp = stage.compute_definition_fingerprint()
    input_fingerprints_by_index = _compute_input_fingerprints(queueable)
    entries = stage_cache.find_entries(project, sid, stage_fp)
    entries_by_fingerprint = {entry.input_fingerprint: entry for entry in entries}

    pending, decided = _split_pending_and_decided(
        queueable, input_fingerprints_by_index, entries_by_fingerprint
    )

    _record_queue_stats(accumulation, queueable, passthrough, pending, decided)

    if len(pending):
        pending_fingerprints = input_fingerprints_by_index.loc[pending.index].tolist()
        _snapshot_pending_and_halt(ctx, sid, pending, stage_fp, pending_fingerprints, accumulation)

    decided = _collect_cached_output_rows(decided, entries_by_fingerprint, input_fingerprints_by_index)
    passthrough = _finalize_passthrough_rows(passthrough)
    out = _combine_decided_and_passthrough(decided, passthrough)
    return _project_onto_output_schema(out, stage, accumulation)


# --- handle_human_review_queue helpers -----------------------------------------


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


def _auto_approve_all(
    src: pd.DataFrame, stage: Stage, accumulation: StageAccumulation
) -> pd.DataFrame:
    """Pass every row through as approved, entirely in memory — no stage-cache
    decision lookup, no queue snapshot, no halt. Each row gets the same reviewer
    columns an 'approve' decision produces (final/human score = ai score), then the
    frame is projected onto the output schema exactly as the real path's output would
    be, so the stage's recorded row count is its real one. No human reviewed these
    rows, so reviewer_id/reviewed_at stay null."""
    approved = src.copy()
    ai = approved[_SCORE_COLUMN] if _SCORE_COLUMN in approved.columns else pd.NA
    approved["ai_score"] = ai
    approved["human_score"] = ai
    approved["final_score"] = ai
    approved["review_notes"] = f"decision={RowReviewDecision.approve}"
    approved["reviewer_id"] = pd.NA
    approved["reviewed_at"] = pd.NA
    approved["decision"] = RowReviewDecision.approve
    return _project_onto_output_schema(approved, stage, accumulation)


def _partition_reviewable_rows(
    src: pd.DataFrame, flt: str | None, sid: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split `src` into rows subject to review (the queue filter matched, or
    there is no filter) and rows that pass straight through."""
    if flt:
        # A PredicateError here means `flt` falls outside the closed grammar
        # parse_predicate enforces (bad grammar) — let it propagate loud rather
        # than folding it into the eval-time except below, so a bad-grammar
        # filter fails at parse rather than being misreported as an eval failure.
        parsed = parse_predicate(flt)
        try:
            # eval of a comparison yields a bool Series; the explicit dtype=bool
            # conversion makes that a checked fact (anything else lands in the
            # except below and is raised as a loud error).
            queueable_mask = pd.Series(
                src.eval(parsed.pandas_expr), index=src.index, dtype=bool
            )
        except (SyntaxError, ValueError, TypeError, KeyError, AttributeError, NameError) as exc:
            # `flt` parses under our grammar but references a column absent
            # from this run's actual frame (or another eval-time problem); a
            # malformed reference must halt the run rather than silently
            # routing every row to passthrough (which would skip human review
            # unnoticed).
            raise ValueError(
                f"human_review_queue '{sid}' filter could not be evaluated: `{flt}` "
                f"({type(exc).__name__}: {exc}). A filter must reference existing input columns."
            ) from exc
    else:
        queueable_mask = pd.Series([True] * len(src), index=src.index)

    return src[queueable_mask].copy(), src[~queueable_mask].copy()


def _compute_input_fingerprints(queueable: pd.DataFrame) -> pd.Series:
    """`input_fingerprint` per row of `queueable`, computed via
    `compute_row_fingerprint` over each row's ORIGINAL upstream columns —
    index-aligned WITH `queueable`, never assigned back onto it as a column.
    A queued row's snapshot (`_snapshot_pending_and_halt`) must carry only its
    original upstream columns, so this fingerprint lives only in this Series
    and, for pending rows, in the sidecar file written alongside the
    snapshot."""
    if not len(queueable):
        return pd.Series([], index=queueable.index, dtype=object)
    fingerprints = queueable.apply(lambda row: compute_row_fingerprint(row.to_dict()), axis=1)
    assert isinstance(fingerprints, pd.Series)
    return fingerprints


def _split_pending_and_decided(
    queueable: pd.DataFrame,
    input_fingerprints_by_index: pd.Series,
    entries_by_fingerprint: dict[str, StageCacheEntry],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """`queueable` split by whether its row's fingerprint (looked up in
    `input_fingerprints_by_index`, never a dataframe column) names a cached
    decision. Both halves keep exactly `queueable`'s own columns — no
    decision-placeholder column is ever seeded, pending or decided."""
    decided_mask = input_fingerprints_by_index.isin(entries_by_fingerprint.keys())
    return queueable[~decided_mask], queueable[decided_mask]


def _record_queue_stats(
    accumulation: StageAccumulation,
    queueable: pd.DataFrame,
    passthrough: pd.DataFrame,
    pending: pd.DataFrame,
    decided: pd.DataFrame,
) -> None:
    """Record this stage's queue tallies onto `accumulation`; the executor drains
    them onto the manifest under `queue_stats[stage_id]`."""
    stats: QueueStats = {
        "items_queued_total": int(len(queueable)),
        "items_passed_through": int(len(passthrough)),
        "items_pending": int(len(pending)),
        "items_decided": int(len(decided)),
    }
    accumulation.queue_stats = stats


def _snapshot_pending_and_halt(
    ctx: RunContext,
    sid: str,
    pending: pd.DataFrame,
    stage_fingerprint: str,
    input_fingerprints: list[str],
    accumulation: StageAccumulation,
) -> NoReturn:
    """Persist the pending items for the reviewer UI, then halt the run.

    The snapshot (`<stage>.parquet`, or `.csv` on a parquet-incompatible
    dtype) is written PURE — exactly `pending`'s own upstream columns, no
    fingerprint or decision-bookkeeping column ever added to it. The
    fingerprints those rows carry are never row data; they live in a sidecar
    `<stage>.fingerprints.json`, `input_fingerprints` POSITIONALLY aligned to
    the snapshot's row order, alongside the one `stage_fingerprint` every
    pending row of this halt shares."""
    queue_dir = ctx.require_run_dir() / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"{sid}.parquet"
    try:
        pending.to_parquet(queue_path, index=False)
    except (pa_lib.ArrowException, ValueError, TypeError):
        # A column whose dtype/shape parquet can't represent (mixed-type
        # object columns, nested Python values) — CSV stringifies those and
        # succeeds. A disk/OS error (ENOSPC, permission) is deliberately NOT
        # caught here: it would fail identically for CSV, so it propagates
        # (and is recorded by the runner) rather than silently degrading the
        # queue snapshot.
        queue_path = queue_dir / f"{sid}.csv"
        pending.to_csv(queue_path, index=False)
    fingerprints_path = queue_dir / f"{sid}.fingerprints.json"
    fingerprints_path.write_text(
        json.dumps({
            "stage_fingerprint": stage_fingerprint,
            "input_fingerprints": input_fingerprints,
        }),
        encoding="utf-8",
    )
    raise HaltForReview(
        stage_id=sid,
        pending_count=int(len(pending)),
        queue_path=queue_path,
        accumulation=accumulation,
    )


def _collect_cached_output_rows(
    decided: pd.DataFrame,
    entries_by_fingerprint: dict[str, StageCacheEntry],
    input_fingerprints_by_index: pd.Series,
) -> pd.DataFrame:
    """Look up each decided row's cached entry and collect the non-tombstone
    output rows into the replacement frame — a tombstone (`output_row is None`)
    drops its row. The entry carries the stage's output for that input; this
    handler neither builds nor interprets it."""
    if not len(decided):
        return decided
    matched = [entries_by_fingerprint[fp] for fp in input_fingerprints_by_index.loc[decided.index]]
    output_rows = [entry.output_row for entry in matched if entry.output_row is not None]
    return pd.DataFrame(output_rows)


def _finalize_passthrough_rows(passthrough: pd.DataFrame) -> pd.DataFrame:
    """Pass-through rows keep their AI score as final; the reviewer columns are
    filled with the pass-through-specific values (no human ever reviewed them)."""
    if len(passthrough) and _SCORE_COLUMN in passthrough.columns:
        passthrough["ai_score"] = passthrough[_SCORE_COLUMN]
        passthrough["final_score"] = passthrough[_SCORE_COLUMN]
    passthrough["human_score"] = pd.NA
    passthrough["reviewer_id"] = passthrough.get("reviewer", pd.NA)
    passthrough["reviewed_at"] = pd.NA
    passthrough["review_notes"] = "below review threshold"
    return passthrough


def _combine_decided_and_passthrough(decided: pd.DataFrame, passthrough: pd.DataFrame) -> pd.DataFrame:
    return pd.concat([decided, passthrough], ignore_index=True, sort=False)


def _project_onto_output_schema(
    out: pd.DataFrame, stage: Stage, accumulation: StageAccumulation
) -> pd.DataFrame:
    """Project onto exactly the columns output_schema declares — a column
    carried through from upstream that the stage wants downstream earns its
    place by being declared. Columns on the frame that the schema doesn't
    declare are dropped, and the drop is recorded on `accumulation` rather than
    silently discarded. Attaches the accumulation to the returned frame so the
    executor can drain it into the manifest."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        out.attrs[ACCUMULATION_ATTR] = accumulation
        return out
    keep = [c for c in declared if c in out.columns]
    dropped = [str(c) for c in out.columns if c not in keep]
    if dropped:
        accumulation.dropped_columns = dropped
    result = out[keep]
    result.attrs[ACCUMULATION_ATTR] = accumulation
    return result
