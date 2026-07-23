"""Handler for the human_review_queue stage type."""

from __future__ import annotations

from typing import NoReturn

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.predicate import parse_predicate
from app.models import RowReviewDecision, Stage
from app.services.stage_cache import ReadOnlyStageCache, StageCacheEntry, compute_row_fingerprint

from ..context import QueueStats, RunContext
from ..errors import HaltForReview


def handle_human_review_queue(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    """Real review-queue semantics:

    1. Apply the queue filter to upstream output → items needing review.
    2. Fingerprint the stage definition once (`stage_fp`) and every queueable
       row (`input_fp`, over its original upstream columns).
    3. Look up this stage definition's cached decisions from `ctx.stage_cache`
       and apply the ones whose `input_fingerprint` matches a queued row:
         - items with a cached decision get it applied
         - items without are written to runs/<id>/queue/<stage>.parquet
    4. If ANY items lack decisions, raise HaltForReview so the runner can
       stop downstream execution and mark the run awaiting_review.
    5. Otherwise return a dataframe with final_score populated (ai if
       approved, human override if modified; rejected rows dropped).
    """
    sid = stage.id
    queue_cfg = stage.queue
    assert queue_cfg is not None  # Stage validation: human_review_queue carries queue_cfg
    project, stage_cache = _require_project_scope(ctx, sid)
    src = inputs[stage.inputs[0].id].copy()

    queueable, passthrough = _partition_reviewable_rows(src, queue_cfg.filter, ctx, sid)

    stage_fp = stage.compute_definition_fingerprint()
    queueable = _fingerprint_queueable_rows(queueable, stage_fp)
    entries = stage_cache.find_entries(project, sid, stage_fp)
    queueable = _apply_cached_decisions(queueable, entries)
    pending, decided = _split_pending_and_decided(queueable)

    _record_queue_stats(ctx, sid, queueable, passthrough, pending, decided)

    if len(pending):
        _snapshot_pending_and_halt(ctx, sid, pending)

    decided = _apply_decided_rows(decided)
    passthrough = _finalize_passthrough_rows(passthrough)
    out = _combine_decided_and_passthrough(decided, passthrough)
    return _project_onto_output_schema(out, stage, ctx, sid)


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


def _partition_reviewable_rows(
    src: pd.DataFrame, flt: str | None, ctx: RunContext, sid: str
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


def _fingerprint_queueable_rows(queueable: pd.DataFrame, stage_fingerprint: str) -> pd.DataFrame:
    """Stamp every queued row with its own `input_fingerprint` — computed via
    `compute_row_fingerprint` over the row's ORIGINAL upstream columns, before
    this function adds any bookkeeping column of its own — and the constant
    `stage_fingerprint` for this stage definition. Both columns survive onto
    the pending-item snapshot (`_snapshot_pending_and_halt`) and are exactly
    the pair `find_entries`/`StageCache.put` key a cache entry by."""
    if len(queueable):
        queueable["input_fingerprint"] = queueable.apply(
            lambda row: compute_row_fingerprint(row.to_dict()), axis=1
        )
    queueable["stage_fingerprint"] = stage_fingerprint
    return queueable


def _apply_cached_decisions(queueable: pd.DataFrame, entries: list[StageCacheEntry]) -> pd.DataFrame:
    """Left-join cached human decisions onto `queueable` by input_fingerprint;
    when there is nothing to join (no queued rows, or no cache entries for
    this stage definition), make sure the decision columns exist so
    downstream code can rely on their presence."""
    if len(queueable) and entries:
        decisions = pd.DataFrame([
            {
                "input_fingerprint": entry.input_fingerprint,
                "decision": entry.human.decision,
                "modified_score": entry.human.modified_score,
                "reviewer": entry.human.reviewer,
                "reviewed_at": entry.human.reviewed_at,
            }
            for entry in entries
        ])
        return queueable.merge(decisions, on="input_fingerprint", how="left")
    for col in ["decision", "modified_score", "reviewer", "reviewed_at"]:
        if col not in queueable.columns:
            queueable[col] = pd.NA
    return queueable


def _split_pending_and_decided(queueable: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    pending = queueable[queueable["decision"].isna()]
    decided = queueable[queueable["decision"].notna()]
    return pending, decided


def _record_queue_stats(
    ctx: RunContext,
    sid: str,
    queueable: pd.DataFrame,
    passthrough: pd.DataFrame,
    pending: pd.DataFrame,
    decided: pd.DataFrame,
) -> None:
    """Stats for the manifest."""
    stats: QueueStats = {
        "items_queued_total": int(len(queueable)),
        "items_passed_through": int(len(passthrough)),
        "items_pending": int(len(pending)),
        "items_decided": int(len(decided)),
    }
    ctx.queue_stats[sid] = stats


def _snapshot_pending_and_halt(ctx: RunContext, sid: str, pending: pd.DataFrame) -> NoReturn:
    """Persist the pending items for the reviewer UI, then halt the run."""
    queue_dir = ctx.run_dir / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"{sid}.parquet"
    # Persist a snapshot — everything needed for the reviewer UI plus the
    # input_fingerprint/stage_fingerprint pair so a decision recorded against
    # this snapshot can be cached against the same key.
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
    raise HaltForReview(
        stage_id=sid,
        pending_count=int(len(pending)),
        queue_path=queue_path,
    )


def _apply_review_decision(row: pd.Series) -> pd.Series:
    """One decided row's human-reviewed score columns, derived from its stored
    `decision`."""
    ai = row.get("score")
    decision = row.get("decision")
    if decision == RowReviewDecision.modify:
        final = row.get("modified_score")
        human = row.get("modified_score")
    elif decision == RowReviewDecision.reject:
        final = pd.NA
        human = pd.NA
    else:  # approve
        final = ai
        human = ai
    row["ai_score"] = ai
    row["human_score"] = human
    row["final_score"] = final
    row["review_notes"] = f"decision={decision}"
    return row


def _apply_decided_rows(decided: pd.DataFrame) -> pd.DataFrame:
    """All items have decisions — apply them and drop rejected rows (final_score
    is NA) from the output."""
    if not len(decided):
        return decided
    decided = decided.apply(_apply_review_decision, axis=1)
    # Row-wise _apply_review_decision returns Series rows, so apply(axis=1)
    # builds a DataFrame — the stubs can't see that, so check it at runtime.
    assert isinstance(decided, pd.DataFrame)
    return decided[decided["decision"] != RowReviewDecision.reject].copy()


def _finalize_passthrough_rows(passthrough: pd.DataFrame) -> pd.DataFrame:
    """Pass-through rows keep their AI score as final; the reviewer columns are
    filled with the pass-through-specific values (no human ever reviewed them)."""
    if len(passthrough) and "score" in passthrough.columns:
        passthrough["ai_score"] = passthrough["score"]
        passthrough["final_score"] = passthrough["score"]
    passthrough["human_score"] = pd.NA
    passthrough["reviewer_id"] = passthrough.get("reviewer", pd.NA)
    passthrough["reviewed_at"] = pd.NA
    passthrough["review_notes"] = "below review threshold"
    return passthrough


def _combine_decided_and_passthrough(decided: pd.DataFrame, passthrough: pd.DataFrame) -> pd.DataFrame:
    if "reviewer" in decided.columns:
        decided = decided.rename(columns={"reviewer": "reviewer_id"})
    return pd.concat([decided, passthrough], ignore_index=True, sort=False)


def _project_onto_output_schema(
    out: pd.DataFrame, stage: Stage, ctx: RunContext, sid: str
) -> pd.DataFrame:
    """Project onto exactly the columns output_schema declares — a column
    carried through from upstream that the stage wants downstream earns its
    place by being declared. Columns on the frame that the schema doesn't
    declare are dropped, and the drop is recorded on `ctx.dropped_columns`
    rather than silently discarded."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        return out
    keep = [c for c in declared if c in out.columns]
    dropped = [c for c in out.columns if c not in keep]
    if dropped:
        ctx.dropped_columns[sid] = dropped
    return out[keep]
