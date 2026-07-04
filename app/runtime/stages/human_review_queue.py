"""Handler for the human_review_queue stage type."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pandas as pd

from ._shared import HaltForReview, _translate_where


def _content_hash(row: pd.Series, columns: list[str]) -> str:
    """Stable hash of the listed column values for one row. Used to match
    queue items across re-runs so prior human decisions can be reapplied
    even when upstream non-determinism shuffles primary keys."""
    parts = [str(row.get(c, "")) for c in columns]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _hash_columns_for(stage: dict[str, Any]) -> list[str]:
    """Columns to include in the content hash. Falls back to the upstream
    input's primary_key if `queue.hash_columns` isn't set."""
    queue = stage.get("queue") or {}
    cols = queue.get("hash_columns")
    if cols:
        return list(cols)
    inputs = stage.get("inputs") or []
    if inputs and isinstance(inputs[0], dict):
        pk = (inputs[0].get("schema") or {}).get("primary_key") or []
        if pk:
            return list(pk)
    return []


def _decisions_path(ctx: dict[str, Any], stage_id: str) -> Path:
    methodology_dir: Path = ctx["methodology_dir"]
    d = methodology_dir / "decisions"
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


def handle_human_review_queue(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
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
    sid = stage["id"]
    inps = stage.get("inputs", [])
    src = inputs[inps[0]["id"]].copy()
    queue_cfg = stage.get("queue") or {}
    flt = queue_cfg.get("filter")

    # Partition rows: those subject to review vs. those passing through.
    if flt:
        try:
            # eval of a comparison yields a bool Series; the explicit dtype=bool
            # conversion makes that a checked fact (anything else lands in the
            # except below and is recorded as a filter_error).
            queueable_mask = pd.Series(
                src.eval(_translate_where(flt)), index=src.index, dtype=bool
            )
        except Exception:
            queueable_mask = pd.Series([False] * len(src), index=src.index)
            ctx.setdefault("queue_stats", {}).setdefault(sid, {})[
                "filter_error"
            ] = f"could not evaluate `{flt}`"
    else:
        queueable_mask = pd.Series([True] * len(src), index=src.index)

    queueable = src[queueable_mask].copy()
    passthrough = src[~queueable_mask].copy()

    hash_cols = _hash_columns_for(stage)
    if not hash_cols:
        raise ValueError(
            f"Queue stage '{sid}' has no hash_columns and no upstream primary_key; "
            "cannot match items across runs."
        )
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
        except Exception:
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
        if decision == "modify":
            final = row.get("modified_score")
            human = row.get("modified_score")
        elif decision == "reject":
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
        decided = decided[decided["decision"] != "reject"].copy()

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

    declared = [c["name"] for c in (stage.get("output_schema") or {}).get("columns", [])]
    if declared:
        keep = [c for c in declared if c in out.columns]
        for must_keep in ["entity_id", "evidence_id", "benchmark_id", "query_id", "quote"]:
            if must_keep in out.columns and must_keep not in keep:
                keep.append(must_keep)
        out = out[keep]
    return out
