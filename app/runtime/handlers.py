"""
Stage handlers — one per stage type.

Each handler takes (stage_yaml, inputs_dict_of_dataframes, run_context) and
returns a pandas DataFrame (or None if the stage produced side-effect artifacts
only). The runner dispatches on stage type.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from .llm_mock import mock_llm_call


class HaltForReview(Exception):
    """Raised by handle_human_review_queue when there are pending items
    without human decisions. The runner catches this, marks the run as
    awaiting_review, and stops executing downstream stages."""

    def __init__(self, stage_id: str, pending_count: int, queue_path: Path):
        super().__init__(
            f"Stage '{stage_id}' has {pending_count} item(s) awaiting review"
        )
        self.stage_id = stage_id
        self.pending_count = pending_count
        self.queue_path = queue_path


def handle_input_data(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    connector = stage.get("connector", {})
    kind = connector.get("kind")
    params = connector.get("params", {})

    if kind == "file":
        path = ctx["repo_root"] / params["path"]
        fmt = params.get("format", "csv")
        if fmt == "csv":
            df = pd.read_csv(path)
        elif fmt == "parquet":
            df = pd.read_parquet(path)
        elif fmt == "json":
            df = pd.read_json(path, lines=True)
        else:
            raise ValueError(f"Unsupported file format: {fmt}")

        # Optional list-column splitting (e.g., "[a, b]" → ["a", "b"])
        for col in params.get("list_columns", []):
            if col in df.columns:
                df[col] = df[col].apply(_parse_list_cell)

        # Optional date parsing
        for col in params.get("parse_dates", []):
            if col in df.columns:
                df[col] = pd.to_datetime(df[col], errors="coerce")

        return df

    if kind == "computed_static":
        # Demo mode: read from the file param if provided
        path = params.get("file")
        if path:
            return pd.read_csv(ctx["repo_root"] / path)
        return pd.DataFrame()

    if kind in {"scrape", "http", "api", "manual_upload", "sql"}:
        # Production-only connectors. In the prototype we expect these stages
        # to have been replaced with a `file` connector pointing to a sample.
        raise NotImplementedError(
            f"Connector kind '{kind}' is not implemented in the demo runtime. "
            f"Stage '{stage['id']}' should use kind=file pointing to a local sample."
        )

    raise ValueError(f"Unknown connector kind: {kind}")


def handle_python_transform(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    fn_spec = stage.get("function") or {}
    kind = fn_spec.get("kind")
    if kind == "module":
        module_name = fn_spec["module"]
        fn_name = fn_spec.get("function", "transform")
        module = importlib.import_module(module_name)
        fn = getattr(module, fn_name)
    elif kind == "inline":
        code = fn_spec.get("code", "")
        ns: dict[str, Any] = {}
        exec(code, ns)
        fn_name = fn_spec.get("function", "transform")
        fn = ns.get(fn_name) or ns.get("transform")
        if fn is None:
            raise ValueError(f"Inline function 'transform' not defined for stage {stage['id']}")
    else:
        raise ValueError(f"Unknown function kind for stage {stage['id']}: {kind}")

    # Pass dataframes positionally in declared input order.
    args = [inputs[inp["id"]] for inp in stage.get("inputs", [])]
    return fn(*args)


def handle_join(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    join_cfg = stage.get("join", {})
    inps = stage.get("inputs", [])
    if len(inps) < 2:
        raise ValueError(f"join stage {stage['id']} needs >=2 inputs")
    left = inputs[inps[0]["id"]]
    right = inputs[inps[1]["id"]]
    keys = join_cfg.get("keys") or join_cfg.get("on") or []
    how = join_cfg.get("type", "inner")
    left_keys = [k["left"] for k in keys]
    right_keys = [k["right"] for k in keys]
    if not left_keys:
        raise ValueError(f"join stage {stage['id']} has no keys configured")

    merged = left.merge(right, left_on=left_keys, right_on=right_keys, how=how, suffixes=("", "_r"))

    select = join_cfg.get("select")
    if select:
        existing = [c for c in select if c in merged.columns]
        merged = merged[existing]
    return merged


def handle_aggregate(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    agg_cfg = stage.get("aggregate", {})
    inps = stage.get("inputs", [])
    df = inputs[inps[0]["id"]]
    group_by = agg_cfg.get("group_by", [])
    aggs = agg_cfg.get("aggregations", [])

    rows = df.copy()
    # Apply per-aggregation `where` filters by computing each aggregation
    # separately then merging.
    results = None
    for op in aggs:
        out = op["output_column"]
        formula = op["formula"]
        value = op.get("value_column")
        weight = op.get("weight_column")
        where = op.get("where")
        slice_df = rows
        if where:
            slice_df = rows.query(_translate_where(where))

        if formula in {"sum", "mean", "count", "min", "max"}:
            if formula == "count":
                series = slice_df.groupby(group_by, dropna=False).size().rename(out)
            else:
                series = slice_df.groupby(group_by, dropna=False)[value].agg(formula).rename(out)
        elif formula == "weighted_mean":
            slice_df = slice_df.dropna(subset=[value])
            slice_df["_weighted"] = slice_df[value] * slice_df[weight]
            num = slice_df.groupby(group_by, dropna=False)["_weighted"].sum()
            den = slice_df.groupby(group_by, dropna=False)[weight].sum()
            series = (num / den).rename(out)
        elif formula == "weighted_sum":
            series = slice_df.groupby(group_by, dropna=False).apply(
                lambda g: (g[value] * g[weight]).sum() if value else g[weight].sum()
            ).rename(out)
        elif formula == "first":
            series = slice_df.groupby(group_by, dropna=False)[value].first().rename(out)
        elif formula == "list":
            series = slice_df.groupby(group_by, dropna=False)[value].apply(list).rename(out)
        else:
            raise ValueError(f"Unknown aggregation formula: {formula}")

        partial = series.reset_index()
        results = partial if results is None else results.merge(partial, on=group_by, how="outer")

    return results if results is not None else pd.DataFrame(columns=group_by)


def handle_llm_transform(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    llm = stage.get("llm", {})
    inps = stage.get("inputs", [])
    if not inps:
        raise ValueError(f"llm_transform stage {stage['id']} has no inputs")
    src = inputs[inps[0]["id"]]
    out_rows = []

    for _, row in src.iterrows():
        row_dict = row.to_dict()
        result = mock_llm_call(stage["id"], llm, row_dict)

        # If the mock returned a list (e.g. evidence_extraction can produce
        # multiple evidence pieces per document), emit one output row per
        # element. Otherwise emit one row.
        if isinstance(result, list):
            for idx, item in enumerate(result):
                merged = {**row_dict, **item}
                merged["evidence_id"] = _evidence_id_for(row_dict, idx)
                out_rows.append(merged)
        elif isinstance(result, dict):
            merged = {**row_dict, **result}
            out_rows.append(merged)
        else:
            out_rows.append({**row_dict, "_raw": str(result)})

    df = pd.DataFrame(out_rows)
    # Keep only columns declared in output_schema, preserving order, plus any
    # passthrough columns that schema declared with source: passthrough.
    declared = [c["name"] for c in (stage.get("output_schema") or {}).get("columns", [])]
    if declared:
        keep = [c for c in declared if c in df.columns]
        # Also keep stable id columns commonly used downstream
        for must_keep in ["evidence_id", "doc_id", "entity_id", "source_class", "published_at",
                          "benchmark_id", "query_id"]:
            if must_keep in df.columns and must_keep not in keep:
                keep.append(must_keep)
        df = df[keep]
    return df


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
            queueable_mask = src.eval(_translate_where(flt))
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
        snapshot_cols = list(pending.columns)
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


def handle_publish(stage: dict[str, Any], inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    """Publish stages have a function: block. Run the function and capture its
    output dataframe (paths to artifacts)."""
    fn_spec = stage.get("function")
    if fn_spec is None:
        raise ValueError(f"publish stage {stage['id']} requires a function: block")
    # Pass inputs positionally + an output_dir kwarg
    publish_cfg = stage.get("publish", {})
    output_dir = publish_cfg.get("destination", "build/")
    output_dir = str(ctx["run_dir"] / "artifacts" / Path(output_dir).name)

    module_name = fn_spec["module"]
    fn_name = fn_spec.get("function", "transform")
    module = importlib.import_module(module_name)
    fn = getattr(module, fn_name)

    args = [inputs[inp["id"]] for inp in stage.get("inputs", [])]
    return fn(*args, output_dir=output_dir)


HANDLERS = {
    "input_data": handle_input_data,
    "python_transform": handle_python_transform,
    "join": handle_join,
    "aggregate": handle_aggregate,
    "llm_transform": handle_llm_transform,
    "human_review_queue": handle_human_review_queue,
    "publish": handle_publish,
}


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _parse_list_cell(cell: Any) -> list[str]:
    if isinstance(cell, list):
        return cell
    if pd.isna(cell):
        return []
    s = str(cell).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [x.strip() for x in s.split(",") if x.strip()]


def _translate_where(expr: str) -> str:
    """Translate our SQL-ish predicate dialect to pandas eval syntax.

    Wraps AND/OR operands in parens so bitwise &/| binds the right way,
    and lowercases boolean literals."""
    import re
    e = expr
    e = e.replace(" IS NOT NULL", ".notna()")
    e = e.replace(" IS NULL", ".isna()")
    e = re.sub(r"\btrue\b", "True", e, flags=re.IGNORECASE)
    e = re.sub(r"\bfalse\b", "False", e, flags=re.IGNORECASE)

    def _split_wrap(s: str, sep: str, joiner: str) -> str:
        parts = [p.strip() for p in re.split(rf"\s+{sep}\s+", s)]
        if len(parts) <= 1:
            return s
        return f" {joiner} ".join(f"({p})" for p in parts)

    e = _split_wrap(e, "OR", "|")
    e = _split_wrap(e, "AND", "&")
    return e


def _evidence_id_for(row: dict[str, Any], idx: int) -> str:
    base = row.get("doc_id") or row.get("evidence_id") or "anon"
    return f"{base}#{idx}"
