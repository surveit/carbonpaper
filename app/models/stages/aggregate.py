"""Column validation for an aggregate stage, on both the input and output
side: `group_by`, each aggregation's `value_column`, and every column an
aggregation's `where` predicate references must resolve against the stage's
input edge; and a declared output_schema must be deliverable by the columns
group_by + the aggregations actually produce."""
from __future__ import annotations

from typing import TYPE_CHECKING

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

DUPLICATE_GROUP_BY_ISSUE = (
    "stage '{sid}': aggregate.group_by names column '{col}' more than once; the "
    "group-by cannot produce one column twice"
)
DUPLICATE_AGGREGATION_ISSUE = (
    "stage '{sid}': aggregate.aggregations declare output_column '{col}' more than "
    "once; the handle merges its per-aggregation results, so neither lands under "
    "that name"
)
AGGREGATION_SHADOWS_GROUP_BY_ISSUE = (
    "stage '{sid}': aggregation output_column '{col}' is also a group_by column; the "
    "handle cannot produce both under one name"
)


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
    the derivation can know the type.

    Output-column collisions inside the config itself are checked first and
    short-circuit: they are undeliverable regardless of what (if anything) the
    stage declares, and they make the name→type derivation a fiction."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    collisions = find_aggregate_output_collisions(stage.id, aggregate)
    if collisions or stage.output_schema is None:
        return collisions
    edge = stage.inputs[0].table_schema
    derived = derive_aggregate_output_types(aggregate, edge)
    return find_declared_vs_derived_issues(stage.id, "aggregate", stage.output_schema, derived)


def find_aggregate_output_collisions(stage_id: str, aggregate: "AggregateConfig") -> list[str]:
    """One issue per output column two parts of the config both claim: a
    `group_by` entry repeated, two aggregations sharing an `output_column`, or an
    aggregation shadowing a group_by column.

    Each is undeliverable, and none is caught downstream today: the handle
    groups then outer-merges its per-aggregation frames on `group_by`, so two
    aggregations named `total` land as pandas' `total_x`/`total_y` and NEITHER
    declared name exists, while a repeated group_by or a shadowed group_by
    column raises `cannot insert <col>, already exists` mid-run."""
    issues: list[str] = []
    seen_group_by: set[str] = set()
    for column in aggregate.group_by:
        if column in seen_group_by:
            issues.append(DUPLICATE_GROUP_BY_ISSUE.format(sid=stage_id, col=column))
        seen_group_by.add(column)
    seen_outputs: set[str] = set()
    for op in aggregate.aggregations:
        if op.output_column in seen_outputs:
            issues.append(DUPLICATE_AGGREGATION_ISSUE.format(sid=stage_id, col=op.output_column))
        elif op.output_column in seen_group_by:
            issues.append(
                AGGREGATION_SHADOWS_GROUP_BY_ISSUE.format(sid=stage_id, col=op.output_column)
            )
        seen_outputs.add(op.output_column)
    return issues


def derive_aggregate_output_types(
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
