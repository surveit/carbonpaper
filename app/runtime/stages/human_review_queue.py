"""Handler for the human_review_queue stage type."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.lib as pa_lib

from app.core.predicate import parse_predicate
from app.models import RowReviewDecision, Stage

from ..errors import HaltForReview


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
    flt = queue_cfg.filter

    # Partition rows: those subject to review vs. those passing through.
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

    queueable = src[queueable_mask].copy()
    passthrough = src[~queueable_mask].copy()

    hash_cols = stage.resolve_hash_columns()
    # Stage validation guarantees a human_review_queue resolves a non-empty hash
    # source — queue.hash_columns or the upstream primary_key (see
    # Stage._queue_has_resolvable_hash) — so this documents the invariant rather
    # than handling a reachable case. The missing-column check below still bites:
    # the model only checks columns when the upstream schema is DECLARED, whereas
    # this checks the ACTUAL frame.
    assert hash_cols, f"queue stage '{sid}' has no resolvable hash columns"
    missing = [c for c in hash_cols if c not in queueable.columns]
    if missing:
        raise ValueError(
            f"Queue stage '{sid}': hash columns missing from upstream: {missing}"
        )

    if len(queueable):
        queueable["content_hash"] = queueable.apply(
            lambda r: _content_hash(r, hash_cols), axis=1
        )

    # Look up prior decisions.
    decisions = _load_decisions(ctx, sid)
    if len(queueable) and len(decisions):
        queueable = queueable.merge(
            decisions[["content_hash", "decision", "modified_score",
                       "reviewer", "reviewed_at"]],
            on="content_hash", how="left",
        )
    else:
        for col in ["decision", "modified_score", "reviewer", "reviewed_at"]:
            if col not in queueable.columns:
                queueable[col] = pd.NA

    pending = queueable[queueable["decision"].isna()]
    decided = queueable[queueable["decision"].notna()]

    # Stats for the manifest.
    ctx.setdefault("queue_stats", {})[sid] = {
        "items_queued_total": int(len(queueable)),
        "items_passed_through": int(len(passthrough)),
        "items_pending": int(len(pending)),
        "items_decided": int(len(decided)),
    }

    # If anything's pending, snapshot the queue and halt the run.
    if len(pending):
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

    # All items have decisions — apply them and emit the output frame.
    def _apply(row: pd.Series) -> pd.Series:
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

    if len(decided):
        decided = decided.apply(_apply, axis=1)
        # Row-wise _apply returns Series rows, so apply(axis=1) builds a
        # DataFrame — the stubs can't see that, so check it at runtime.
        assert isinstance(decided, pd.DataFrame)
        # Drop rejected rows from the output (final_score is NA).
        decided = decided[decided["decision"] != RowReviewDecision.reject].copy()

    # Pass-through rows: keep ai score as final.
    if len(passthrough) and "score" in passthrough.columns:
        passthrough["ai_score"] = passthrough["score"]
        passthrough["final_score"] = passthrough["score"]
    passthrough["human_score"] = pd.NA
    passthrough["reviewer_id"] = passthrough.get("reviewer", pd.NA)
    passthrough["reviewed_at"] = pd.NA
    passthrough["review_notes"] = "below review threshold"

    if "reviewer" in decided.columns:
        decided = decided.rename(columns={"reviewer": "reviewer_id"})

    out = pd.concat([decided, passthrough], ignore_index=True, sort=False)

    # Project onto exactly the columns output_schema declares — a column carried
    # through from upstream that the stage wants downstream earns its place by
    # being declared. Columns on the frame that the schema doesn't declare are
    # dropped, and the drop is recorded on ctx rather than silently discarded.
    declared = [c.name for c in stage.output_schema.columns] if stage.output_schema else []
    if declared:
        keep = [c for c in declared if c in out.columns]
        dropped = [c for c in out.columns if c not in keep]
        if dropped:
            ctx.setdefault("dropped_columns", {})[sid] = dropped
        out = out[keep]
    return out
