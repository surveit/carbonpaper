"""Every stage type said in English, for a reader who does not write workflows."""

from __future__ import annotations

from pathlib import PurePath
from typing import assert_never

from pydantic import BaseModel

from app.models.stages.aggregate import AggFormula, AggregateStage, AggregationOp
from app.models.stages.dedupe import DedupeStage
from app.models.stages.input_data import InputDataStage
from app.models.stages.join import EnrichStage, ExpandStage
from app.models.stages.stage_base import AbstractStage, StageType


def say_what_a_stage_did(stage: AbstractStage) -> str:
    """The mechanism, for a line sitting under the stage's own description."""
    match stage.type:
        case StageType.input_data:
            return _say_the_load(stage)
        case StageType.union:
            return f"Stack {_and_list(_input_ids(stage))} into one table"
        case StageType.enrich | StageType.expand:
            return _say_the_join(stage)
        case StageType.aggregate:
            return _say_the_grouping(stage)
        case StageType.dedupe:
            return _say_the_collapse(stage)
        case StageType.filter_rows | StageType.starlark_filter_rows:
            return "Test every row and keep the ones that pass"
        case StageType.llm_transform:
            return "Ask an AI model about each row"
        case StageType.python_row_function | StageType.starlark_row_function:
            return "Run code once per row"
        case StageType.python_frame_function:
            return "Run code over the whole table at once"
        case StageType.explode:
            return "Give every value in a list column a row of its own"
        case StageType.sort_rank:
            return "Put the rows in order and number them"
        case StageType.human_review_queue:
            return "Hand the rows to a person to decide"
        case StageType.publish:
            return "Write the figures out"
    assert_never(stage.type)


class AggregationOutput(BaseModel):
    """One output column, said as a sentence the source column sits inside."""

    column: str
    does: str
    from_column: str | None
    # The clause after the source column, where the formula owes one.
    then: str | None


class AggregatePlan(BaseModel):
    lead: str
    grouped_by: list[str]
    outputs: list[AggregationOutput]


def plan_an_aggregate(stage: AggregateStage) -> AggregatePlan:
    keys = list(stage.aggregate.group_by)
    return AggregatePlan(
        lead=(f"One row per {_and_list(keys)}."
              if keys else "Every row collapses into one row."),
        grouped_by=keys,
        outputs=[_say_the_output(op) for op in stage.aggregate.aggregations],
    )


def _say_the_output(op: AggregationOp) -> AggregationOutput:
    does, then = _say_the_formula(op.formula)
    return AggregationOutput(
        column=op.output_column, does=does, from_column=op.value_column, then=then)


def _say_the_formula(formula: AggFormula) -> tuple[str, str | None]:
    match formula:
        case AggFormula.sum:
            return "added up from", None
        case AggFormula.mean:
            return "the average of", None
        case AggFormula.count_:
            return "how many rows there were", None
        case AggFormula.count_distinct:
            return "how many different values of", None
        case AggFormula.min:
            return "the smallest", None
        case AggFormula.max:
            return "the largest", None
        case AggFormula.list:
            return "every", ", kept as a list"
        case AggFormula.only:
            return "carried from", " — the run stops if two rows of the group differ"
        case AggFormula.first | AggFormula.first_including_null:
            return "whichever row came first", None
    assert_never(formula)


def _say_the_load(stage: AbstractStage) -> str:
    if not isinstance(stage, InputDataStage):
        return "Read the rows in"
    named = [PurePath(path).name for path in stage.connector.params.paths]
    return f"Load input data from {_and_list(named)}" if named else "Load input data"


def _say_the_join(stage: AbstractStage) -> str:
    if not isinstance(stage, (EnrichStage, ExpandStage)):
        return "Combine two tables"
    inputs = _input_ids(stage)
    pairs = [key.left if key.left == key.right else f"{key.left} = {key.right}"
             for key in stage.join.keys]
    subject = inputs[0] if inputs else "these rows"
    reference = inputs[1] if len(inputs) > 1 else "the reference table"
    on = f" on {_and_list(pairs)}" if pairs else ""
    if stage.type is StageType.expand:
        return f"Combine {subject} data with {reference} data{on}, one row per match"
    return f"Combine {subject} data with {reference} data{on}"


def _say_the_grouping(stage: AbstractStage) -> str:
    if not isinstance(stage, AggregateStage):
        return "Collapse the rows"
    keys = list(stage.aggregate.group_by)
    if not keys:
        return "Collapse every row into a single row of figures"
    return f"Group the rows by {_and_list(keys)}, one row out per group"


def _say_the_collapse(stage: AbstractStage) -> str:
    if not isinstance(stage, DedupeStage):
        return "Collapse the repeated rows"
    return f"Collapse rows that share the same {_and_list(list(stage.dedupe.keys))}"


def _input_ids(stage: AbstractStage) -> list[str]:
    return [str(read.id) for read in stage.inputs]


def _and_list(names: list[str]) -> str:
    if len(names) < 3:
        return " and ".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"
