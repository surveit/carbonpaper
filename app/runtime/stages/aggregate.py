"""Handler for the aggregate stage type."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa

from app.core.predicate import parse_predicate
from app.core.frames import table_to_frame
from app.models import Stage
from app.models.stages.aggregate import (
    AGG_FORMULA_COUNT,
    AGG_FORMULA_COUNT_DISTINCT,
    AGG_FORMULA_FIRST,
    AGG_FORMULA_LIST,
    AggregateStage,
    AggregationOp,
)

from ..context import RunContext
from ..stage_output import StageOutput
from ..lineage import grouped_contributions_lineage
from .execution import narrow_stage

# Carries each input row's ordinal through the same grouping the numbers go
# through, so provenance is read off the aggregation itself rather than worked
# out again. Reserved `_`-prefixed namespace, which a stage may never declare.
ORDINAL_KEY = "_trace_aggregate_ord"


def handle_aggregate(stage: Stage, inputs: dict[str, pa.Table], ctx: RunContext) -> StageOutput:
    agg_cfg = narrow_stage(stage, AggregateStage).aggregate
    input_id = stage.inputs[0].id
    df = table_to_frame(inputs[input_id])
    if not agg_cfg.aggregations:
        return StageOutput.from_frame(pd.DataFrame(columns=agg_cfg.group_by))

    rows = df.copy()
    rows[ORDINAL_KEY] = np.arange(len(df))
    if agg_cfg.group_by:
        results, contributors = _aggregate_by_group(rows, agg_cfg.group_by, agg_cfg.aggregations)
    else:
        results, contributors = _reduce_whole_frame(rows, agg_cfg.aggregations)
    return StageOutput.from_frame(
        results, lineage=grouped_contributions_lineage(input_id, contributors))


def _aggregate_by_group(
    rows: pd.DataFrame, group_by: list[str], aggregations: list[AggregationOp]
) -> tuple[pd.DataFrame, list[dict[int, tuple[str, ...]]]]:
    results: pd.DataFrame | None = None
    # Each aggregation is computed separately and the partials merged on the
    # group keys, because each `where` picks its own rows.
    ordinals_by_op: list[pd.DataFrame] = []
    for op in aggregations:
        slice_df = _rows_admitted_by(rows, op)
        partial = _grouped_value(slice_df, group_by, op).reset_index()
        results = partial if results is None else results.merge(partial, on=group_by, how="outer")
        ordinals_by_op.append(
            slice_df.groupby(group_by, dropna=False)[ORDINAL_KEY]
            .apply(list).rename(op.output_column).reset_index()
        )
    assert results is not None  # the caller returns before this on no aggregations
    return results, _contributors_per_group(results, group_by, ordinals_by_op)


def _reduce_whole_frame(
    rows: pd.DataFrame, aggregations: list[AggregationOp]
) -> tuple[pd.DataFrame, list[dict[int, tuple[str, ...]]]]:
    """`group_by: []` — the frame is ONE group, so exactly one row comes out, empty input or not."""
    columns: dict[str, Any] = {}
    contributors: dict[int, list[str]] = {}
    for op in aggregations:
        slice_df = _rows_admitted_by(rows, op)
        columns[op.output_column] = _whole_frame_value(slice_df, op)
        for ordinal in slice_df[ORDINAL_KEY]:
            contributors.setdefault(int(ordinal), []).append(op.output_column)
    results = pd.DataFrame({name: [value] for name, value in columns.items()})
    return results, [{o: tuple(cols) for o, cols in sorted(contributors.items())}]


def _rows_admitted_by(rows: pd.DataFrame, op: AggregationOp) -> pd.DataFrame:
    """The rows this aggregation's `where` admits; every row where it declares none."""
    if not op.where:
        return rows
    return rows.query(parse_predicate(op.where).pandas_expr)


def _grouped_value(slice_df: pd.DataFrame, group_by: list[str], op: AggregationOp) -> pd.Series:
    out = op.output_column
    if op.formula == AGG_FORMULA_COUNT:
        return slice_df.groupby(group_by, dropna=False).size().rename(out)
    grouped = slice_df.groupby(group_by, dropna=False)[_value_column(op)]
    if op.formula in {"sum", "mean", "min", "max"}:
        return grouped.agg(op.formula).rename(out)
    if op.formula == AGG_FORMULA_FIRST:
        return grouped.first().rename(out)
    if op.formula == AGG_FORMULA_COUNT_DISTINCT:
        # dropna=True is pandas' default, passed explicitly because it is the
        # semantics being chosen: a null is the absence of a value, so it is
        # not one of the distinct values (SQL's COUNT(DISTINCT col)). A group
        # whose every value is null therefore counts 0, not 1.
        return grouped.nunique(dropna=True).rename(out)
    if op.formula == AGG_FORMULA_LIST:
        return grouped.apply(list).rename(out)
    raise ValueError(f"Unknown aggregation formula: {op.formula}")


def _whole_frame_value(slice_df: pd.DataFrame, op: AggregationOp) -> Any:
    """Each formula over the slice; an EMPTY slice is null, whatever the formula."""
    if slice_df.empty:
        # 0 is an outcome — it claims something was measured and found to be
        # none. Nothing was measured here, so the honest answer is absence.
        # This is also what the grouped path emits: a group no row survived is
        # missing from that aggregation's partial, and the outer merge fills it
        # null. Emptiness is emptiness whether it came from a `where` that
        # admitted no row or from an input frame that held none.
        return np.nan
    if op.formula == AGG_FORMULA_COUNT:
        return len(slice_df)
    values = slice_df[_value_column(op)]
    if op.formula in {"sum", "mean", "min", "max"}:
        return getattr(values, op.formula)()
    if op.formula == AGG_FORMULA_FIRST:
        return _first_present(values)
    if op.formula == AGG_FORMULA_COUNT_DISTINCT:
        return values.nunique(dropna=True)
    if op.formula == AGG_FORMULA_LIST:
        return list(values)
    raise ValueError(f"Unknown aggregation formula: {op.formula}")


def _first_present(values: pd.Series) -> Any:
    """Matches groupby.first(): the first NON-null value, null where the slice holds none."""
    present = values.dropna()
    return present.iloc[0] if len(present) else np.nan


def _value_column(op: AggregationOp) -> str:
    if op.value_column is None:
        raise ValueError(
            f"aggregation `{op.output_column}`: formula `{op.formula}` needs value_column")
    return op.value_column


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
