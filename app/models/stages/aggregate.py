"""aggregate stage: the config block, plus column validation on both the
input and output side — `group_by`, each aggregation's `value_column`, and
every column an aggregation's `where` references must resolve against what
the stage's input supplies; and the signature's `produces` must be exactly what
group_by + the aggregations compute."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Literal, Optional, Sequence

from pydantic import Field, model_validator

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate
from app.models.schema import StageConfig, TableSchema, _Base
from app.models.stages.stage_base import AbstractStage, StageInput, StageType
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_computed_issues,
    find_predicate_column_issues,
    resolve_input_columns,
)
from app.models.stages.stage_type_spec import StageTypeSpec
from app.models.stages.signature import ReplacesSignature

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStageInput


class AggFormula(str, Enum):
    sum = "sum"
    mean = "mean"
    count_ = "count"  # trailing underscore: `count` would shadow str.count
    count_distinct = "count_distinct"
    min = "min"
    max = "max"
    first = "first"
    first_including_null = "first_including_null"
    list = "list"


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


class AggregateConfig(StageConfig):
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"group_by", "aggregations"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset()

    group_by: list[str]
    aggregations: list[AggregationOp]


class AggregateStage(AbstractStage):
    type: Literal[StageType.aggregate]
    CACHE_IGNORED_BECAUSE: ClassVar[str] = (
        "a vectorised aggregation costs less than hashing its own input would"
    )
    aggregate: AggregateConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1, max_length=1)
    signature: ReplacesSignature

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"aggregate": self.aggregate}

    def find_config_column_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_aggregate_column_issues(self, inputs)

    def find_signature_schema_issues(
        self, inputs: Sequence["WorkflowStageInput"]
    ) -> list[str]:
        return find_aggregate_signature_issues(self, inputs)


# Aggregation formula names, compared as plain strings both by
# compute_aggregate_output_types below and by the runtime handler
# (app.runtime.stages.aggregate.handle_aggregate, which executes the same
# dispatch on real data) — named here so the two sites can't drift apart.
AGG_FORMULA_COUNT = "count"
AGG_FORMULA_COUNT_DISTINCT = "count_distinct"
AGG_FORMULA_FIRST = "first"
AGG_FORMULA_FIRST_INCLUDING_NULL = "first_including_null"
AGG_FORMULA_LIST = "list"


def find_aggregate_column_issues(
    stage: "AggregateStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    aggregate = stage.aggregate
    cols = resolve_input_columns(inputs, 0)
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


def find_aggregate_signature_issues(
    stage: "AggregateStage", inputs: Sequence["WorkflowStageInput"]
) -> list[str]:
    signature = stage.signature
    aggregate = stage.aggregate
    input_id = inputs[0].id

    declared = {
        column.name
        for entry in signature.reads
        if entry.input == input_id
        for column in entry.columns
    }
    consumed = set(aggregate.group_by)
    consumed.update(op.value_column for op in aggregate.aggregations if op.value_column)
    for op in aggregate.aggregations:
        if op.where:
            try:
                consumed.update(parse_predicate(op.where, declared).columns)
            except PredicateError:
                pass  # find_aggregate_column_issues already reports the bad predicate
    issues = [
        f"stage '{stage.id}': signature reads `{name}` but the aggregate config "
        f"never consumes it"
        for name in sorted(declared - consumed)
    ]
    issues.extend(
        f"stage '{stage.id}': the aggregate config consumes `{name}` but the "
        f"signature does not read it"
        for name in sorted(consumed - declared)
    )

    computed = compute_aggregate_output_types(aggregate, inputs[0].table_schema)
    issues.extend(find_declared_vs_computed_issues(
        stage.id, "aggregate signature",
        TableSchema(columns=signature.produces), computed,
    ))
    produced = {column.name for column in signature.produces}
    issues.extend(
        f"stage '{stage.id}': the aggregate config emits `{name}` but the "
        f"signature's produces omits it"
        for name in sorted(set(computed) - produced)
    )
    return issues


def compute_aggregate_output_types(
    aggregate: "AggregateConfig", edge: "TableSchema"
) -> dict[str, str | None]:
    def edge_type(name: str | None) -> str | None:
        if name is None:
            return None
        column = edge.column_for_name(name)
        return column.type if column is not None else None

    computed: dict[str, str | None] = {g: edge_type(g) for g in aggregate.group_by}
    for op in aggregate.aggregations:
        value_type = edge_type(op.value_column)
        if op.formula in (AGG_FORMULA_COUNT, AGG_FORMULA_COUNT_DISTINCT):
            computed[op.output_column] = "int"
        elif op.formula == "mean":
            computed[op.output_column] = "float"
        elif op.formula == "sum":
            # str is in the set because a pandas sum over strings concatenates them.
            computed[op.output_column] = (
                value_type if value_type in ("int", "float", "str") else None
            )
        elif op.formula == AGG_FORMULA_LIST:
            computed[op.output_column] = f"list[{value_type}]" if value_type else None
        else:  # min / max / first / first_including_null: the value column's own type
            computed[op.output_column] = value_type
    return computed

# Authoring copy for this module's stage type(s); assembled into STAGE_TYPES.
STAGE_TYPE_SPECS: dict[str, StageTypeSpec] = {
    "aggregate": StageTypeSpec(
        summary="Structured group-by aggregation.",
        signature_form="replaces",
        blocks=["aggregate"],
        requires_inputs=True,
        min_inputs=1,
        required=["group_by", "aggregations"],
        optional=[],
        notes=(
            "Output columns are exactly group_by plus each aggregation's output_column — every "
            "other input column is DROPPED, so carry anything needed downstream via "
            "group_by or an aggregation. `first` takes the first NON-NULL value; "
            "`first_including_null` takes the first row's value, including NULL. "
            "An EMPTY group_by "
            "reduces the whole frame "
            "to ONE row of just the aggregation outputs — reach for it whenever a stage "
            "boils everything down to published figures. That row comes out even when no "
            "row reaches it, every figure NULL. formula `count` counts ROWS and takes no "
            "value_column; every other formula requires one — `count_distinct` counts "
            "distinct NON-NULL values. Declared output types must match what the formula "
            "computes: count/count_distinct->int, mean->float, "
            "min/max/first/first_including_null->the value column's type, "
            "list->list[<that type>]."
        ),
    ),
}
