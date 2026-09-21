"""What a stage's own config says a step did, in the words the told figure uses."""

from __future__ import annotations

from typing import Optional

from app.models.stages.aggregate import AggFormula
from app.models.stages.stage_base import StageType
from app.models.workflow_stage import WorkflowStage

# A formula in this set combines nothing: the group-by is asserting its rows agreed.
_ASSERTS = frozenset({AggFormula.only.value, AggFormula.list.value,
                      AggFormula.count_.value, AggFormula.count_distinct.value})

_SAID_FORMULAS = {
    AggFormula.sum.value: "summed",
    AggFormula.mean.value: "averaged",
    AggFormula.min.value: "the smallest of them",
    AggFormula.max.value: "the largest of them",
    AggFormula.count_.value: "counted",
    AggFormula.count_distinct.value: "counted as distinct values",
    AggFormula.only.value: "required to agree",
    AggFormula.list.value: "listed",
}


def read_the_predicate(stage: WorkflowStage) -> Optional[str]:
    """The authored verb phrase for what this step tests; None where nobody wrote one."""
    for holder in ("filter", "starlark_filter", "queue"):
        block = getattr(stage.stage, holder, None)
        written = getattr(block, "predicate", None) if block is not None else None
        if written:
            return str(written)
    return None


def name_the_columns_tested(stage: WorkflowStage) -> list[str]:
    """The columns a decision consumed, which are never the ones its rows' value came through."""
    signature = getattr(stage.stage, "signature", None)
    return [column.name
            for read in getattr(signature, "reads", [])
            for column in read.columns]


def say_the_line_lower(description: str) -> str:
    """An authored line dropped mid-sentence, with its own capital only where it is a name."""
    head = description.split(" ", 1)[0]
    if not head[:1].isalpha() or head.isupper() or "_" in head:
        return description
    return description[0].lower() + description[1:]


def name_the_columns_written(stage: WorkflowStage) -> tuple[list[str], list[str]]:
    """What a transform added and what it rewrote, off the signature it declared."""
    signature = getattr(stage.stage, "signature", None)
    adds = [column.name for column in getattr(signature, "adds", [])]
    rewrites = [column.name for column in getattr(signature, "rewrites", [])]
    return adds, rewrites


def writes_columns(stage: WorkflowStage) -> bool:
    adds, rewrites = name_the_columns_written(stage)
    return bool(adds or rewrites)


def reviews_every_row(stage: WorkflowStage) -> bool:
    """A queue that selects or routes put only some rows in front of a person."""
    queue = getattr(stage.stage, "queue", None)
    if queue is None:
        return True
    return not (queue.filter or queue.routing)


def name_the_group_keys(stage: WorkflowStage) -> list[str]:
    aggregate = getattr(stage.stage, "aggregate", None)
    if aggregate is not None:
        return list(aggregate.group_by)
    repeats = getattr(stage.stage, "dedupe", None)
    return list(repeats.keys) if repeats is not None else []


def says_nothing_was_combined(stage: WorkflowStage) -> bool:
    """A dedupe combines nothing by construction; an aggregate, only where every formula does."""
    if StageType(stage.stage.type) is StageType.dedupe:
        return True
    return all(formula in _ASSERTS for formula in list_the_formulas(stage))


def list_the_formulas(stage: WorkflowStage) -> list[str]:
    aggregate = getattr(stage.stage, "aggregate", None)
    if aggregate is None:
        return []
    return [str(op.formula) for op in aggregate.aggregations]


def say_the_formulas(stage: WorkflowStage) -> str:
    """Only the formulas that combine: the ones that assert are not what happened to a value."""
    combining = [formula for formula in list_the_formulas(stage)
                 if formula not in _ASSERTS]
    said = list(dict.fromkeys(_SAID_FORMULAS[formula] for formula in combining))
    return _say_and_list(said)


def find_the_formula_behind(
    stage: WorkflowStage, column: str
) -> Optional[tuple[Optional[str], str]]:
    """The column an aggregation read and what it did to it, for the cited column."""
    aggregate = getattr(stage.stage, "aggregate", None)
    if aggregate is None:
        return None
    for op in aggregate.aggregations:
        if op.output_column == column:
            return op.value_column, _SAID_FORMULAS[str(op.formula)]
    return None


def name_the_looked_up_columns(stage: WorkflowStage) -> list[str]:
    """What an enrich lands on each row; empty for an expand, which lands rows, not columns."""
    join = getattr(stage.stage, "join", None)
    if join is None or StageType(stage.stage.type) is StageType.expand:
        return []
    return list(join.enrich_with.values())


def name_the_reference_input(stage: WorkflowStage) -> Optional[str]:
    """The input a join reads FROM: its second, the subject being its first."""
    inputs = stage.inputs
    return inputs[1].id if len(inputs) > 1 else None


def _say_and_list(said: list[str]) -> str:
    if len(said) <= 1:
        return "".join(said)
    return f"{', '.join(said[:-1])} and {said[-1]}"
