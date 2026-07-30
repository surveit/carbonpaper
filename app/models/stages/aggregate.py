"""aggregate stage: the aggregate handle and its formula vocabulary, plus column
validation on both sides — `group_by`, each aggregation's `value_column`, and
every column a `where` predicate references must resolve against the stage's
input edge, and a declared output_schema must be deliverable by the columns
group_by + the aggregations actually produce."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Optional

from pydantic import model_validator

from app.models.schema import _Base
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_derived_issues,
    find_predicate_column_issues,
    resolve_input_columns,
)

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage import Stage


class AggFormula(str, Enum):
    sum = "sum"
    mean = "mean"
    count_ = "count"  # trailing underscore: `count` would shadow str.count
    min = "min"
    max = "max"
    first = "first"
    list = "list"


# The two formula names both the derivation below and the runtime handler
# (app.runtime.stages.aggregate.handle_aggregate, which executes the same
# dispatch on real data) branch on. Plain strings off the enum: `_Base` sets
# use_enum_values, so a validated `formula` is a str, and the runtime compares
# against the same two constants so the dispatches can't drift.
AGG_FORMULA_COUNT = AggFormula.count_.value
AGG_FORMULA_LIST = AggFormula.list.value


class AggregationOp(_Base):
    output_column: str
    formula: AggFormula
    value_column: Optional[str] = None
    where: Optional[str] = None

    @model_validator(mode="after")
    def _value_column_for_formula(self) -> "AggregationOp":
        if self.formula != AggFormula.count_ and not self.value_column:
            raise ValueError(
                f"aggregation `{self.output_column}`: formula `{self.formula}` needs value_column"
            )
        return self


class AggregateConfig(_Base):
    """aggregate handle."""
    # Every field changes what this stage computes (grouping, aggregations) —
    # see Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"group_by", "aggregations"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    group_by: list[str]
    aggregations: list[AggregationOp]


def find_aggregate_column_issues(stage: "Stage") -> list[str]:
    """Every `group_by` entry, aggregation `value_column`, and column an
    aggregation's `where` references that is absent from the resolved single
    input."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    cols = resolve_input_columns(stage, 0)
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
    derivation contradicts. Type checks apply only where the derivation can know
    the type."""
    aggregate = stage.aggregate
    assert aggregate is not None  # Stage._handle_for_type guarantees this for type="aggregate"
    assert stage.output_schema is not None  # Stage._schemas_declared guarantees this off publish
    edge = stage.inputs[0].table_schema
    derived = derive_aggregate_output_types(aggregate, edge)
    return find_declared_vs_derived_issues(stage.id, "aggregate", stage.output_schema, derived)


def derive_aggregate_output_types(
    aggregate: AggregateConfig, edge: "TableSchema"
) -> dict[str, str | None]:
    """The columns the aggregate handle emits, each mapped to its derived type
    (None = unknowable): every group_by column carries its edge type through
    unchanged, and each aggregation's output column follows its formula —
    count->int and mean->float unconditionally; sum->the value column's type
    for int/float and for str (pandas sum of strings concatenates them);
    min/max/first->the value column's type; list->list[<value column's
    type>]."""
    def edge_type(name: str | None) -> str | None:
        if name is None:
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
