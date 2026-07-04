"""Handler for the aggregate stage type."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ._shared import _translate_where


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
