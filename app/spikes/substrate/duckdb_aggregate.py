"""Spike (issue #194): the `aggregate` stage as ONE DuckDB SQL statement.

`aggregate` is the relational stage worth prototyping first: it is the one that
carries a `where` predicate (so it exercises the issue's parse invariant) and
the one whose pandas implementation is least like what it computes.
`app.runtime.stages.aggregate` runs **one group-by per aggregation** and stitches
the results together with successive outer merges, because pandas has no way to
express "count these rows but sum only those". SQL does, and has since 2003:
the `FILTER (WHERE ...)` aggregate clause. The whole stage becomes one
statement, one scan, and one artifact you can print into a run's lineage:

    SELECT "country",
           count(*) FILTER (WHERE (amount > 1000)) AS "big_payments",
           list("name" ORDER BY "__row_number") AS "names"
    FROM stage_input
    GROUP BY "country"
    ORDER BY "country"

Five behavioural differences from the pandas handler were found by running both
over the same fixtures (`tests/spikes/test_duckdb_aggregate.py` pins each):

1. **Group set.** N per-aggregation group-bys outer-merged give the *union* of
   each filtered slice's groups; one filtered scan gives the input's groups. A
   group no aggregation's filter admits vanishes from the pandas result and
   survives (with `count` = 0) here. SQL's answer is the defensible one — "how
   many big payments did Ireland make" has answer 0, not "no row".
2. **Null counts.** A `count` whose group is missing from another aggregation's
   slice comes back `NaN` from the outer merge, which also drags the whole
   column from `int64` to `float64` — a `count` column that fails an `int`
   output_schema. `count(*) FILTER (...)` is `0` and stays `int64`.
3. **`first` needs an explicit order.** pandas' `GroupBy.first()` means "first
   non-null in input order"; SQL aggregates have no inherent order. The
   generated SQL therefore carries an explicit `ORDER BY __row_number` (a
   column this module attaches to the input, so the ordering is the *input's*,
   not the scan's) and a null-skipping filter — matching pandas deliberately
   rather than by luck. `list` is ordered the same way, and unlike pandas it
   carries real `None` elements instead of `float('nan')`.
4. **Group order.** pandas' `groupby` returns groups sorted by key; DuckDB's is
   unordered unless asked. The generated statement therefore ends in an
   explicit `ORDER BY` over the group keys — run output has to be reproducible.
5. **`sum` over a `str` column.** pandas concatenates (and
   `derive_aggregate_output_types` documents that); DuckDB's binder refuses it.
   Refusing is the better answer, but it *is* a behaviour change, and any
   workflow relying on it breaks at the migration rather than at review.

The output frame is Arrow-typed pandas (`pd.ArrowDtype`), not numpy-backed, so
the result carries real nulls and a real schema.
"""
from __future__ import annotations

from typing import Mapping

import duckdb
import pandas as pd
import pyarrow as pa

from app.models import AggregateConfig, AggFormula, AggregationOp, Stage

from .sql_predicate import parse_sql_predicate

# The relation the generated statement reads. A fixed name: the statement is
# built for a single registered input, never composed across stages.
STAGE_INPUT_RELATION = "stage_input"

# Column this module attaches to the input to carry *input row order* into SQL,
# where aggregates are otherwise unordered. Dunder-free but underscore-prefixed
# so it cannot collide with an authored column, and never projected out.
ROW_ORDER_COLUMN = "__row_number"

# Each formula → the DuckDB aggregate function that computes it. `mean` is SQL's
# `avg`; `count` takes no value column. Keeping this a table (rather than a
# chain of `==` comparisons) is what makes "every formula is handled" checkable
# by iterating the enum.
_AGGREGATE_FUNCTIONS: dict[AggFormula, str] = {
    AggFormula.sum: "sum",
    AggFormula.mean: "avg",
    AggFormula.count_: "count",
    AggFormula.min: "min",
    AggFormula.max: "max",
    AggFormula.first: "first",
    AggFormula.list: "list",
}

# Formulas whose SQL must be given the input's row order explicitly, because
# their pandas counterpart is order-sensitive and SQL aggregation is not.
_ORDER_SENSITIVE_FORMULAS = frozenset({AggFormula.first, AggFormula.list})

# Formulas whose pandas counterpart skips nulls in a way SQL does not reproduce
# on its own. `GroupBy.first()` returns the first NON-NULL value, so the SQL
# gets a matching null-skipping filter.
_NULL_SKIPPING_FORMULAS = frozenset({AggFormula.first})


def aggregate_sql(aggregate: AggregateConfig, *, relation: str = STAGE_INPUT_RELATION) -> str:
    """The single SQL statement this aggregate config compiles to.

    Pure — no connection, no data — so it is directly assertable in a test and
    directly recordable as a stage's lineage. Each aggregation's `where` is
    embedded as the SQL *rendered back from the parse that validated it*
    (`sql_predicate.parse_sql_predicate`), which is the issue's invariant made
    operational: the string in this statement cannot be a different expression
    from the one whose columns were checked.
    """
    group_by = [_quote_identifier(name) for name in aggregate.group_by]
    projected = group_by + [_aggregation_sql(op) for op in aggregate.aggregations]
    statement = f"SELECT {', '.join(projected)}\nFROM {relation}"
    if group_by:
        # ORDER BY, not just GROUP BY: pandas' groupby sorts by key and a run's
        # output must be reproducible, so the ordering is stated rather than
        # inherited from whatever order the scan happened to produce.
        statement += f"\nGROUP BY {', '.join(group_by)}\nORDER BY {', '.join(group_by)}"
    return statement


def run_aggregate_duckdb(stage: Stage, inputs: Mapping[str, pd.DataFrame]) -> pd.DataFrame:
    """Execute an aggregate stage on DuckDB, returning an Arrow-typed frame.

    Signature-compatible with `app.runtime.stages.aggregate.handle_aggregate`
    minus its unused `ctx`, so the two can be run head to head on the same
    inputs — which is exactly what the spike's tests do.
    """
    aggregate = stage.aggregate
    if aggregate is None:
        raise ValueError(f"stage {stage.id}: not an aggregate stage")
    source = _with_row_order(pa.Table.from_pandas(inputs[stage.inputs[0].id], preserve_index=False))
    con = duckdb.connect()
    try:
        con.register(STAGE_INPUT_RELATION, source)
        result = con.execute(aggregate_sql(aggregate)).to_arrow_table()
    finally:
        con.close()
    return result.to_pandas(types_mapper=pd.ArrowDtype)


def _with_row_order(table: pa.Table) -> pa.Table:
    """`table` plus `ROW_ORDER_COLUMN` — the input's row order, materialised as
    data so the generated SQL can order by it. Attached in Arrow rather than as
    `row_number() OVER ()` in SQL: a window function's ordering depends on the
    scan, this does not."""
    order = pa.array(range(table.num_rows), type=pa.int64())
    return table.append_column(ROW_ORDER_COLUMN, order)


def _aggregation_sql(op: AggregationOp) -> str:
    """One aggregation as a projected SQL expression with its alias."""
    formula = AggFormula(op.formula)
    function = _AGGREGATE_FUNCTIONS[formula]
    argument = _aggregate_argument(op, formula)
    filters = _aggregate_filters(op, formula)
    expression = f"{function}({argument})"
    if filters:
        expression += f" FILTER (WHERE {' AND '.join(filters)})"
    return f"{expression} AS {_quote_identifier(op.output_column)}"


def _aggregate_argument(op: AggregationOp, formula: AggFormula) -> str:
    """What the aggregate function is applied to: `*` for `count`, otherwise the
    value column — carrying an explicit `ORDER BY` for the order-sensitive
    formulas."""
    if formula is AggFormula.count_:
        return "*"
    if op.value_column is None:
        raise ValueError(f"aggregation `{op.output_column}`: formula `{op.formula}` needs value_column")
    value = _quote_identifier(op.value_column)
    if formula in _ORDER_SENSITIVE_FORMULAS:
        return f"{value} ORDER BY {_quote_identifier(ROW_ORDER_COLUMN)}"
    return value


def _aggregate_filters(op: AggregationOp, formula: AggFormula) -> list[str]:
    """The `FILTER (WHERE ...)` conjuncts for one aggregation: its authored
    `where` (rendered from the parse that validated it) plus, for a
    null-skipping formula, the null guard that makes SQL match pandas."""
    filters: list[str] = []
    if op.where:
        filters.append(f"({parse_sql_predicate(op.where).sql})")
    if formula in _NULL_SKIPPING_FORMULAS and op.value_column is not None:
        filters.append(f"({_quote_identifier(op.value_column)} IS NOT NULL)")
    return filters


def _quote_identifier(name: str) -> str:
    """A SQL identifier, double-quoted with embedded quotes doubled — so a
    column named `order` or `a"b` is a name, never syntax."""
    escaped = name.replace('"', '""')
    return f'"{escaped}"'
