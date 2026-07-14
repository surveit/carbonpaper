"""Handler for the aggregate stage type."""

from __future__ import annotations

from typing import Any, Hashable

import pandas as pd

from app.models import Stage

from ..lineage import Edge, record_edges
from ._shared import _translate_where

# Sentinel standing in for a NaN/None group-key component so that rows sharing a
# missing key (grouped together under `dropna=False`) match each other — NaN != NaN
# would otherwise scatter them.
_NULL_KEY = object()


def _norm_key(values: Any) -> tuple[Hashable, ...]:
    """Normalise a group key (scalar for a single group_by column, tuple for
    several) into a hashable tuple, mapping every NaN/None component to a shared
    sentinel so missing-key groups compare equal."""
    if not isinstance(values, tuple):
        values = (values,)
    return tuple(_NULL_KEY if pd.isna(v) else v for v in values)


def handle_aggregate(stage: Stage, inputs: dict[str, pd.DataFrame], ctx: dict[str, Any]) -> pd.DataFrame:
    agg_cfg = stage.aggregate
    assert agg_cfg is not None  # Stage validation: aggregate carries agg_cfg
    input_id = stage.inputs[0].id
    df = inputs[input_id]
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

    results = results if results is not None else pd.DataFrame(columns=group_by)

    _record_aggregate_lineage(ctx, stage.id, input_id, rows, results, group_by)
    return results


def _record_aggregate_lineage(
    ctx: dict[str, Any],
    stage_id: str,
    input_id: str,
    rows: pd.DataFrame,
    results: pd.DataFrame,
    group_by: list[str],
) -> None:
    """Record output→input edges for an aggregate stage.

    An output row is one *group*; it traces to every input row that *belongs* to
    that group — i.e. shares its group key over the FULL, unfiltered input. This
    is deliberately independent of any per-aggregation `where` filter: a `where`
    narrows which rows feed a *formula*, not which rows belong to a *group* (the
    show-your-work design's group-vs-formula distinction). NaN group keys are
    grouped under `dropna=False` and matched via a shared sentinel.
    """
    rows_reset = rows.reset_index(drop=True)
    # Positional membership of each group key over the full input.
    indices_by_key: dict[tuple[Hashable, ...], list[int]] = {}
    for key, positions in rows_reset.groupby(group_by, dropna=False).indices.items():
        indices_by_key[_norm_key(key)] = [int(p) for p in positions]

    edges: list[Edge] = []
    group_cols = results[group_by]
    for out_row in range(len(results)):
        key = _norm_key(tuple(group_cols.iloc[out_row]))
        for in_row in indices_by_key.get(key, []):
            edges.append((out_row, input_id, in_row))
    record_edges(ctx, stage_id, edges)
