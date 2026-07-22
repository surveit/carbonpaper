"""Handler for the human_review_queue stage type."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, NoReturn

import pandas as pd
import pyarrow.lib as pa_lib

from app.models import RowReviewDecision, Stage

from ..errors import HaltForReview
from ._shared import _translate_where


def handle_human_review_queue(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Real review-queue semantics:

    1. Apply the queue filter to upstream output → items needing review.
    2. Hash each item by `queue.hash_columns` (default: upstream PK).
    3. Match against the global decisions store keyed by content_hash:
         - items with prior decisions get them applied
         - items without are written to runs/<id>/queue/<stage>.parquet
    4. If ANY items lack decisions, raise HaltForReview so the runner can
       stop downstream execution and mark the run awaiting_review.
    5. Otherwise return a dataframe with final_score populated (ai if
       approved, human override if modified; rejected rows dropped).
    """
    sid = stage.id
    queue_cfg = stage.queue
    assert queue_cfg is not None  # Stage validation: human_review_queue carries queue_cfg
    src = inputs[stage.inputs[0].id].copy()

    queueable, passthrough = _partition_reviewable_rows(src, queue_cfg.filter, ctx, sid)

    hash_cols = stage.resolve_hash_columns()
    # Stage validation guarantees a human_review_queue resolves a non-empty hash
    # source — queue.hash_columns or the upstream primary_key (see
    # Stage._queue_has_resolvable_hash) — so this documents the invariant rather
    # than handling a reachable case.
    assert hash_cols, f"queue stage '{sid}' has no resolvable hash columns"
    queueable = _hash_queueable_rows(queueable, hash_cols, sid)

    decisions = _load_decisions(ctx, sid)
    queueable = _apply_prior_decisions(queueable, decisions)
    pending, decided = _split_pending_and_decided(queueable)

    _record_queue_stats(ctx, sid, queueable, passthrough, pending, decided)

    if len(pending):
        _snapshot_pending_and_halt(ctx, sid, pending)

    decided = _apply_decided_rows(decided)
    passthrough = _finalize_passthrough_rows(passthrough)
    out = _combine_decided_and_passthrough(decided, passthrough)
    return _project_onto_output_schema(out, stage, ctx, sid)


# --- handle_human_review_queue helpers -----------------------------------------


def _content_hash(row: pd.Series, columns: list[str]) -> str:
    """Stable hash of the listed column values for one row. Used to match
    queue items across re-runs so prior human decisions can be reapplied
    even when upstream non-determinism shuffles primary keys."""
    parts = [str(row.get(c, "")) for c in columns]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _decisions_path(ctx: dict[str, Any], stage_id: str) -> Path:
    project_dir: Path = ctx["project_dir"]
    d = project_dir / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stage_id}.parquet"


def _load_decisions(ctx: dict[str, Any], stage_id: str) -> pd.DataFrame:
    p = _decisions_path(ctx, stage_id)
    if not p.exists():
        return pd.DataFrame(
            columns=["content_hash", "decision", "modified_score",
                     "reviewer", "reviewed_at", "source_run_id"]
        )
    return pd.read_parquet(p)


def _partition_reviewable_rows(
    src: pd.DataFrame, flt: str | None, ctx: dict[str, Any], sid: str
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split `src` into rows subject to review (the queue filter matched, or
    there is no filter) and rows that pass straight through."""
    if flt:
        try:
            # eval of a comparison yields a bool Series; the explicit dtype=bool
            # conversion makes that a checked fact (anything else lands in the
            # except below and is recorded as a filter_error).
            queueable_mask = pd.Series(
                src.eval(_translate_where(flt)), index=src.index, dtype=bool
            )
        except (SyntaxError, ValueError, TypeError, KeyError, AttributeError, NameError):
            # `flt` is an arbitrary author-supplied expression (queue.filter
            # in the stage YAML); a malformed one must not crash the run —
            # everything falls through to the queue instead, and the
            # specific error is recorded below for the author to fix.
            queueable_mask = pd.Series([False] * len(src), index=src.index)
            ctx.setdefault("queue_stats", {}).setdefault(sid, {})[
                "filter_error"
            ] = f"could not evaluate `{flt}`"
    else:
        queueable_mask = pd.Series([True] * len(src), index=src.index)

    return src[queueable_mask].copy(), src[~queueable_mask].copy()


def _hash_queueable_rows(queueable: pd.DataFrame, hash_cols: list[str], sid: str) -> pd.DataFrame:
    """Content-hash every queued row over `hash_cols`, after checking those
    columns are actually present on the upstream frame — the stage model only
    checks them when the upstream schema is DECLARED, whereas this checks the
    ACTUAL frame."""
    missing = [c for c in hash_cols if c not in queueable.columns]
    if missing:
        raise ValueError(
            f"Queue stage '{sid}': hash columns missing from upstream: {missing}"
        )
    if len(queueable):
        queueable["content_hash"] = queueable.apply(
            lambda r: _content_hash(r, hash_cols), axis=1
        )
    return queueable


def _apply_prior_decisions(queueable: pd.DataFrame, decisions: pd.DataFrame) -> pd.DataFrame:
    """Left-join prior human decisions onto `queueable` by content_hash; when
    there is nothing to join (no queued rows, or none decided yet), make sure
    the decision columns exist so downstream code can rely on their presence."""
    if len(queueable) and len(decisions):
        return queueable.merge(
            decisions[["content_hash", "decision", "modified_score",
                       "reviewer", "reviewed_at"]],
            on="content_hash", how="left",
        )
    for col in ["decision", "modified_score", "reviewer", "reviewed_at"]:
        if col not in queueable.columns:
            queueable[col] = pd.NA
    return queueable


def _split_pending_and_decided(queueable: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    pending = queueable[queueable["decision"].isna()]
    decided = queueable[queueable["decision"].notna()]
    return pending, decided


def _record_queue_stats(
    ctx: dict[str, Any],
    sid: str,
    queueable: pd.DataFrame,
    passthrough: pd.DataFrame,
    pending: pd.DataFrame,
    decided: pd.DataFrame,
) -> None:
    """Stats for the manifest."""
    ctx.setdefault("queue_stats", {})[sid] = {
        "items_queued_total": int(len(queueable)),
        "items_passed_through": int(len(passthrough)),
        "items_pending": int(len(pending)),
        "items_decided": int(len(decided)),
    }


def _snapshot_pending_and_halt(ctx: dict[str, Any], sid: str, pending: pd.DataFrame) -> NoReturn:
    """Persist the pending items for the reviewer UI, then halt the run."""
    queue_dir = ctx["run_dir"] / "queue"
    queue_dir.mkdir(parents=True, exist_ok=True)
    queue_path = queue_dir / f"{sid}.parquet"
    # Persist a snapshot — everything needed for the reviewer UI plus
    # the content_hash so decisions can be recorded against it.
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
    out: pd.DataFrame, stage: Stage, ctx: dict[str, Any], sid: str
) -> pd.DataFrame:
    """Project onto exactly the columns output_schema declares — a column
    carried through from upstream that the stage wants downstream earns its
    place by being declared. Columns on the frame that the schema doesn't
    declare are dropped, and the drop is recorded on ctx rather than silently
    discarded."""
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if not declared:
        return out
    keep = [c for c in declared if c in out.columns]
    dropped = [c for c in out.columns if c not in keep]
    if dropped:
        ctx.setdefault("dropped_columns", {})[sid] = dropped
    return out[keep]
