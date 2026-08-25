"""Which columns of a run's inputs a cited figure needed. docs/branch-analysis.md."""

from __future__ import annotations

from collections import defaultdict
from typing import Collection, Iterable

from app.models.schema import StageId
from app.models.stages.aggregate import AggregateStage
from app.models.stages.dedupe import DedupeStage
from app.models.stages.filter_rows import FilterRowsStage
from app.models.stages.join import JoinStage
from app.models.stages.signature import ExtendsSignature, TransformSignature
from app.models.stages.union import UnionStage
from app.models.workflow import Workflow, sort_stages_by_dependency
from app.models.workflow_stage import WorkflowStage

ColumnsByStage = dict[StageId, set[str]]


def find_columns_behind(workflow: Workflow, on_route: Collection[StageId],
                        cited_stage: StageId, cited_column: str) -> ColumnsByStage:
    """Every column a figure's value passed through, plus what kept its rows in the set."""
    needed: ColumnsByStage = defaultdict(set)
    needed[cited_stage].add(cited_column)
    placed = workflow.index_workflow_stages_by_id()
    for stage_id in on_route:
        if stage_id in placed:
            for source, column in _find_columns_that_chose_the_rows(placed[stage_id]):
                needed[source].add(column)
    for authored in reversed(sort_stages_by_dependency(workflow.stages)):
        here = placed[authored.id]
        for column in sorted(needed.get(authored.id, ())):
            for source, upstream in _find_columns_behind_one(here, column):
                needed[source].add(upstream)
    return dict(needed)


def _find_columns_that_chose_the_rows(here: WorkflowStage
                                      ) -> Iterable[tuple[StageId, str]]:
    """What told this stage's surviving rows from the rest: a predicate, a key, a group."""
    stage = here.stage
    if isinstance(stage, FilterRowsStage):
        yield from _read_columns(stage.signature)
    elif isinstance(stage, JoinStage):
        for key in stage.join.keys:
            yield here.inputs[0].id, key.left
            yield here.inputs[1].id, key.right
    elif isinstance(stage, AggregateStage):
        for group in stage.aggregate.group_by:
            yield here.inputs[0].id, group
    elif isinstance(stage, DedupeStage):
        for deduped_on in stage.dedupe.keys:
            yield here.inputs[0].id, deduped_on


def _find_columns_behind_one(here: WorkflowStage, column: str
                             ) -> Iterable[tuple[StageId, str]]:
    stage = here.stage
    if isinstance(stage, UnionStage):
        yield from ((ref.id, column) for ref in here.inputs)
    elif isinstance(stage, AggregateStage):
        yield from _find_columns_behind_aggregation(here, stage, column)
    elif isinstance(stage.signature, ExtendsSignature):
        yield from _find_columns_behind_extension(here, stage.signature, column)
    elif here.inputs:
        yield from _read_columns(stage.signature)


def _find_columns_behind_extension(here: WorkflowStage, signature: ExtendsSignature,
                                   column: str) -> Iterable[tuple[StageId, str]]:
    written = ({added.name for added in signature.adds}
               | {rewritten.name for rewritten in signature.rewrites})
    stage = here.stage
    if column not in written:
        yield here.inputs[0].id, column
    elif isinstance(stage, JoinStage):
        landed_from = {landed: source for source, landed in stage.join.enrich_with.items()}
        if column in landed_from:
            yield here.inputs[1].id, landed_from[column]
    else:
        yield from _read_columns(signature)


def _find_columns_behind_aggregation(here: WorkflowStage, stage: AggregateStage,
                                     column: str) -> Iterable[tuple[StageId, str]]:
    anchor = here.inputs[0].id
    if column in stage.aggregate.group_by:
        yield anchor, column
    for aggregation in stage.aggregate.aggregations:
        if aggregation.output_column == column and aggregation.value_column:
            yield anchor, aggregation.value_column


def _read_columns(signature: TransformSignature) -> Iterable[tuple[StageId, str]]:
    for reads in signature.reads:
        for read in reads.columns:
            yield reads.input, read.name
