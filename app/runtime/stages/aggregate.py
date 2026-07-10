"""Handler for the aggregate stage type."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.models import AggregateStage

from ._shared import _translate_where


def handle_aggregate(stage: AggregateStage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    agg_cfg = stage.aggregate
    df = inputs[stage.inputs[0].id]
    group_by = agg_cfg.group_by

    rows = df.copy()
    # Apply per-aggregation `where` filters by computing each aggregation
    # separately then merging.
    results = None
    for op in agg_cfg.aggregations:
        out = op.output_column
        formula = op.formula
        where = op.where
        slice_df = rows
        if where:
            slice_df = rows.query(_translate_where(where))

        if formula == "count":
            series = slice_df.groupby(group_by, dropna=False).size().rename(out)
        else:
            if op.value_column is None:
                raise ValueError(f"aggregation `{out}`: formula `{formula}` needs value_column")
            value = op.value_column
            if formula in {"sum", "mean", "min", "max"}:
                series = slice_df.groupby(group_by, dropna=False)[value].agg(formula).rename(out)
            elif formula == "first":
                series = slice_df.groupby(group_by, dropna=False)[value].first().rename(out)
            elif formula == "list":
                series = slice_df.groupby(group_by, dropna=False)[value].apply(list).rename(out)
            else:
                raise ValueError(f"Unknown aggregation formula: {formula}")

        partial = series.reset_index()
        results = partial if results is None else results.merge(partial, on=group_by, how="outer")

    return results if results is not None else pd.DataFrame(columns=group_by)
