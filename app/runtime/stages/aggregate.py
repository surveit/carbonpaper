"""Handler for the aggregate stage type."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.predicate import parse_predicate
from app.models import Stage
from app.models.stages.aggregate import (
    AGG_FORMULA_COUNT,
    AGG_FORMULA_LIST,
    AggregateStage,
)

from ..context import RunContext
from ..lineage import attach_row_lineage, grouped_contributions_lineage
from .execution import narrow_stage

# Carries each input row's ordinal through the same grouping the numbers go
# through, so provenance is read off the aggregation itself rather than worked
# out again. Reserved `_`-prefixed namespace, which a stage may never declare.
ORDINAL_KEY = "_trace_aggregate_ord"


def handle_aggregate(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: RunContext) -> pd.DataFrame:
    agg_cfg = narrow_stage(stage, AggregateStage).aggregate
    input_id = stage.inputs[0].id
    df = inputs[input_id]
    group_by = agg_cfg.group_by

    rows = df.copy()
    rows[ORDINAL_KEY] = np.arange(len(df))
    ordinals_by_op: list[pd.DataFrame] = []
    # Apply per-aggregation `where` filters by computing each aggregation
    # separately then merging.
    results = None
    for op in agg_cfg.aggregations:
        out = op.output_column
        formula = op.formula
        where = op.where
        slice_df = rows
        if where:
            slice_df = rows.query(parse_predicate(where).pandas_expr)

        if formula == AGG_FORMULA_COUNT:
            series = slice_df.groupby(group_by, dropna=False).size().rename(out)
        else:
            if op.value_column is None:
                raise ValueError(f"aggregation `{out}`: formula `{formula}` needs value_column")
            value = op.value_column
            if formula in {"sum", "mean", "min", "max"}:
                series = slice_df.groupby(group_by, dropna=False)[value].agg(formula).rename(out)
            elif formula == "first":
                series = slice_df.groupby(group_by, dropna=False)[value].first().rename(out)
            elif formula == AGG_FORMULA_LIST:
                series = slice_df.groupby(group_by, dropna=False)[value].apply(list).rename(out)
            else:
                raise ValueError(f"Unknown aggregation formula: {formula}")

        partial = series.reset_index()
        results = partial if results is None else results.merge(partial, on=group_by, how="outer")
        ordinals_by_op.append(
            slice_df.groupby(group_by, dropna=False)[ORDINAL_KEY].apply(list).rename(out).reset_index()
        )

    if results is None:
        return pd.DataFrame(columns=group_by)
    return attach_row_lineage(results, grouped_contributions_lineage(
        input_id, _contributors_per_group(results, group_by, ordinals_by_op)))


def _contributors_per_group(
    results: pd.DataFrame, group_by: list[str], ordinals_by_op: list[pd.DataFrame]
) -> list[dict[int, tuple[str, ...]]]:
    """Per output row, each contributing input ordinal mapped to the columns it fed."""
    contributors: list[dict[int, list[str]]] = [{} for _ in range(len(results))]
    # Merged LEFT onto the finished output, so the keys and their order — the NaN
    # group included — are the ones actually emitted. Per op, because each `where`
    # picks its own rows: the rows behind `total` need not be those behind `big_n`.
    for partial in ordinals_by_op:
        column = partial.columns[-1]
        aligned = results[group_by].merge(partial, on=group_by, how="left")[column]
        for out_row in range(len(results)):
            cell = aligned.iat[out_row]
            if not isinstance(cell, list):
                continue  # this group had no row surviving that op's `where`
            for ordinal in cell:
                contributors[out_row].setdefault(int(ordinal), []).append(str(column))
    return [
        {ordinal: tuple(columns) for ordinal, columns in row.items()}
        for row in contributors
    ]
