"""Column validation for an aggregate stage, on both the input and output
side: `group_by`, each aggregation's `value_column`, and every column an
aggregation's `where` predicate references must resolve against the stage's
input edge; and a declared output_schema must be deliverable by the columns
group_by + the aggregations actually produce."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.schema import Column
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_derived_issues,
    find_predicate_column_issues,
    resolve_input_columns,
)

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage import AggregateConfig, Stage

# Aggregation formula names, compared as plain strings both by
# derive_aggregate_output_types below and by the runtime handler
# (app.runtime.stages.aggregate.handle_aggregate, which executes the same
# dispatch on real data) — named here so the two sites can't drift apart.
# Plain string, not the AggFormula enum: AggFormula lives on `Stage` in
# app.models.stage, and importing it here at module scope would be circular
# (stage.py imports this package back for its own model validator).
AGG_FORMULA_COUNT = "count"
AGG_FORMULA_LIST = "list"


def find_aggregate_column_issues(stage: "Stage") -> list[str]:
    """Every `group_by` entry, aggregation `value_column`, and column an
    aggregation's `where` references that is absent from the resolved single
    input; [] when that input's edge declares no schema at all."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    cols = resolve_input_columns(stage, 0)
    if cols is None:
        return []
    issues = [
        COLUMN_ISSUE.format(sid=stage.id, field="aggregate.group_by", col=g, cols=sorted(cols))
        for g in aggregate.group_by
        if g not in cols
    ]
    issues.extend(
        COLUMN_ISSUE.format(
            sid=stage.id,
            field=f"aggregate.aggregations[{op.output_column}].value_column",
            col=op.value_column,
            cols=sorted(cols),
        )
        for op in aggregate.aggregations
        if op.value_column and op.value_column not in cols
    )
    for op in aggregate.aggregations:
        if op.where:
            issues.extend(find_predicate_column_issues(
                op.where, stage_id=stage.id,
                field=f"aggregate.aggregations[{op.output_column}].where", cols=cols,
            ))
    return issues


def find_aggregate_output_issues(stage: "Stage") -> list[str]:
    """Every declared output_schema column the aggregate handle cannot deliver:
    a name outside group_by + aggregation output columns, or a type the
    derivation contradicts. [] when the stage declares no output_schema. Name
    feasibility holds even without an edge schema; type checks apply only where
    the derivation can know the type."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    if stage.output_schema is None:
        return []
    edge = stage.inputs[0].table_schema
    derived = derive_aggregate_output_types(aggregate, edge)
    return find_declared_vs_derived_issues(stage.id, "aggregate", stage.output_schema, derived)


def fill_aggregate_output_columns(stage: "Stage") -> list[Column] | None:
    """The fully-specified output columns this aggregate stage's own handle
    would emit, for the auto-fill seam (app.models.stages.fill_output_schema)
    to use when `stage` declares no output_schema at all. Thin adapter over
    `derive_aggregate_output_columns`: unwraps `stage.aggregate` and the
    single input's edge schema, the same way `find_aggregate_output_issues`
    does for the check-a-declared-schema side. None unless every output
    column is derivable."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    return derive_aggregate_output_columns(aggregate, stage.inputs[0].table_schema)


def derive_aggregate_output_columns(
    aggregate: "AggregateConfig", edge: "TableSchema | None"
) -> list[Column] | None:
    """The fully-specified output columns the aggregate handle emits, or None
    unless every one of them is derivable: each `group_by` entry carries its
    edge `Column` verbatim (type, nullable, enum, range, prose all included);
    each aggregation's output column is a fresh `Column` named after
    `op.output_column`, always `nullable=True`, typed per
    `_partial_aggregate_output_types`'s formula rules. None = not every output
    column is fully derivable: a `group_by` entry with no edge column to carry
    (which an absent `edge` forces whenever `group_by` is non-empty), any
    aggregation whose type is unknowable, or an aggregation `output_column`
    that collides with a `group_by` name -- TableSchema rejects duplicate
    column names outright, so a collision counts as "not derivable" here
    too, consistent with the all-or-nothing contract. A bare `count` with
    no `group_by` needs no value type either, so it derives even with
    `edge=None`."""
    columns: list[Column] = []
    for name in aggregate.group_by:
        source = edge.column_for_name(name) if edge is not None else None
        if source is None:
            return None
        columns.append(source.model_copy())

    types = _partial_aggregate_output_types(aggregate, edge)
    group_by_names = set(aggregate.group_by)
    for op in aggregate.aggregations:
        if op.output_column in group_by_names:
            return None
        op_type = types[op.output_column]
        if op_type is None:
            return None
        columns.append(Column(name=op.output_column, type=op_type, nullable=True))
    return columns


def derive_aggregate_output_types(
    aggregate: "AggregateConfig", edge: "TableSchema | None"
) -> dict[str, str | None]:
    """The columns the aggregate handle emits, each mapped to its derived type
    (None = unknowable). Wraps `derive_aggregate_output_columns`: when every
    output column is fully derivable, its name/type pairs are the answer;
    otherwise falls back to `_partial_aggregate_output_types`, which derives a
    type by name alone wherever it can, even with a partly-unknown edge."""
    columns = derive_aggregate_output_columns(aggregate, edge)
    if columns is not None:
        return {c.name: c.type for c in columns}
    return _partial_aggregate_output_types(aggregate, edge)


def _partial_aggregate_output_types(
    aggregate: "AggregateConfig", edge: "TableSchema | None"
) -> dict[str, str | None]:
    """The columns the aggregate handle emits, each mapped to its derived type
    (None = unknowable): every group_by column carries its edge type through
    unchanged, and each aggregation's output column follows its formula —
    count->int and mean->float unconditionally; sum->the value column's type
    for int/float and for str (pandas sum of strings concatenates them);
    min/max/first->the value column's type; list->list[<value column's
    type>]."""
    def edge_type(name: str | None) -> str | None:
        if name is None or edge is None:
            return None
        column = edge.column_for_name(name)
        return column.type if column is not None else None

    derived: dict[str, str | None] = {g: edge_type(g) for g in aggregate.group_by}
    for op in aggregate.aggregations:
        value_type = edge_type(op.value_column)
        if op.formula == AGG_FORMULA_COUNT:
            derived[op.output_column] = "int"
        elif op.formula == "mean":
            derived[op.output_column] = "float"
        elif op.formula == "sum":
            derived[op.output_column] = (
                value_type if value_type in ("int", "float", "str") else None
            )
        elif op.formula == AGG_FORMULA_LIST:
            derived[op.output_column] = f"list[{value_type}]" if value_type else None
        else:  # min / max / first: the value column's own type
            derived[op.output_column] = value_type
    return derived
