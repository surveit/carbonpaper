"""The backward walk over `signature.reads`, and the writer graph it collapses to."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum
from typing import Collection, Iterable, Sequence

from app.models import AbstractStage, StageType, WorkflowStage
from app.models.schema import StageId
from app.models.stages.aggregate import AggregateStage
from app.models.stages.dedupe import DedupeStage
from app.models.stages.filter_rows import FilterRowsStage
from app.models.stages.join import JoinStage
from app.models.stages.signature import transform_output_schema

WorkflowStagesById = dict[StageId, WorkflowStage]


@dataclass(frozen=True)
class ColumnAt:
    stage_id: StageId
    column: str


class WalkStop(str, Enum):
    """Why a column has no parent: it originates here, or nothing upstream feeds it."""

    root = "root"
    counts_rows = "counts_rows"


@dataclass(frozen=True)
class WalkNode:
    at: ColumnAt
    wrote: bool
    parents: tuple[ColumnAt, ...]
    stop: WalkStop | None


@dataclass(frozen=True)
class ColumnWalk:
    cited: ColumnAt
    nodes: dict[ColumnAt, WalkNode]
    # Hops back from the cited column, shortest path where two reach it.
    depth: dict[ColumnAt, int]

    def list_columns_at(self, stage_id: StageId) -> list[str]:
        return sorted(at.column for at in self.nodes if at.stage_id == stage_id)

    def find_stop_at(self, at: ColumnAt) -> WalkStop | None:
        node = self.nodes.get(at)
        return None if node is None else node.stop


@dataclass(frozen=True)
class WriterEdge:
    from_stage: StageId
    columns: tuple[str, ...]


@dataclass(frozen=True)
class WriterGraph:
    parents: dict[StageId, tuple[WriterEdge, ...]]

    def list_parents(self, stage_id: StageId) -> tuple[WriterEdge, ...]:
        return self.parents.get(stage_id, ())

    def list_stages(self) -> set[StageId]:
        return set(self.parents) | {
            edge.from_stage for edges in self.parents.values() for edge in edges
        }


def walk_column_back(stages: WorkflowStagesById, cited: ColumnAt,
                     also: Sequence[ColumnAt] = ()) -> ColumnWalk:
    """`also` seeds the same walk from further columns; `cited` stays the one it is about."""
    nodes: dict[ColumnAt, WalkNode] = {}
    depth = {at: 0 for at in (cited, *also)}
    queue = deque(depth)
    while queue:
        at = queue.popleft()
        if at in nodes:
            continue
        workflow_stage = stages[at.stage_id]
        parents, stop = _find_parents(workflow_stage, at.column)
        nodes[at] = WalkNode(
            at=at,
            wrote=at.column in list_written_columns(workflow_stage.stage),
            parents=tuple(parents),
            stop=stop,
        )
        for parent in parents:
            depth.setdefault(parent, depth[at] + 1)
            queue.append(parent)
    return ColumnWalk(cited=cited, nodes=nodes, depth=depth)


def find_columns_behind(stages: WorkflowStagesById, on_route: Collection[StageId],
                        cited: ColumnAt) -> dict[StageId, set[str]]:
    """Every column the value passed through, plus what kept its rows in the set."""
    walk = walk_column_back(
        stages, cited, also=list(_find_columns_that_chose_the_rows(stages, on_route)))
    behind: dict[StageId, set[str]] = defaultdict(set)
    for at in walk.nodes:
        behind[at.stage_id].add(at.column)
    return dict(behind)


def _find_columns_that_chose_the_rows(stages: WorkflowStagesById,
                                      on_route: Collection[StageId]
                                      ) -> Iterable[ColumnAt]:
    """What told a stage's surviving rows from the rest: a predicate, a key, a group."""
    for stage_id in on_route:
        placed = stages.get(stage_id)
        if placed is None:
            continue
        stage = placed.stage
        if isinstance(stage, FilterRowsStage):
            yield from _list_read_columns(stage)
        elif isinstance(stage, JoinStage):
            for key in stage.join.keys:
                yield ColumnAt(placed.inputs[0].id, key.left)
                yield ColumnAt(placed.inputs[1].id, key.right)
        elif isinstance(stage, AggregateStage):
            for group in stage.aggregate.group_by:
                yield ColumnAt(placed.inputs[0].id, group)
        elif isinstance(stage, DedupeStage):
            for deduped_on in stage.dedupe.keys:
                yield ColumnAt(placed.inputs[0].id, deduped_on)


def build_writer_graph(walk: ColumnWalk, stages: WorkflowStagesById) -> WriterGraph:
    """Only the stages that WROTE a column on the walk; the rest are contracted out."""
    on_walk = {at.stage_id for at in walk.nodes}
    writers = {at.stage_id for at, node in walk.nodes.items() if node.wrote}
    parents = {}
    for stage_id in sorted(writers):
        edges = tuple(
            WriterEdge(from_stage=upstream, columns=tuple(walk.list_columns_at(upstream)))
            for upstream in _find_writers_upstream(stages, stage_id, writers, on_walk)
        )
        if edges:
            parents[stage_id] = edges
    return WriterGraph(parents=parents)


def find_nearest_writer_upstream(
    walk: ColumnWalk, graph: WriterGraph, stage_id: StageId, column: str
) -> StageId | None:
    """Strictly upstream, so a stage that REWROTE a column sends the reader before it."""
    seen = {stage_id}
    frontier = deque(edge.from_stage for edge in graph.list_parents(stage_id))
    while frontier:
        upstream = frontier.popleft()
        if upstream in seen:
            continue
        seen.add(upstream)
        node = walk.nodes.get(ColumnAt(upstream, column))
        if node is not None and (node.wrote or node.stop is WalkStop.root):
            return upstream
        frontier.extend(edge.from_stage for edge in graph.list_parents(upstream))
    return None


def order_sheet_columns(walk: ColumnWalk) -> list[str]:
    """One order for every step, oldest column first, so a column holds its slot."""
    deepest: dict[str, int] = {}
    for at, depth in walk.depth.items():
        if at.column not in deepest or depth > deepest[at.column]:
            deepest[at.column] = depth
    return sorted(deepest, key=lambda name: (-deepest[name], name))


def list_written_columns(stage: AbstractStage) -> set[str]:
    # Never a report stage, whose empty table the schema call asserts against.
    return {column.name for column in transform_output_schema(stage).columns}


def _find_parents(
    workflow_stage: WorkflowStage, column: str
) -> tuple[list[ColumnAt], WalkStop | None]:
    stage = workflow_stage.stage
    if stage.type == StageType.input_data:
        return [], WalkStop.root
    if column not in list_written_columns(stage):
        return _find_carrying_inputs(workflow_stage, column), None
    if isinstance(stage, AggregateStage):
        collapsed = _find_aggregate_parents(stage, workflow_stage.inputs[0].id, column)
        if collapsed is not None:
            return collapsed
    if isinstance(stage, JoinStage):
        landed = _find_landed_parent(stage, workflow_stage, column)
        if landed is not None:
            return landed
    return _list_read_columns(stage), None


def _find_aggregate_parents(
    stage: AggregateStage, source_id: StageId, column: str
) -> tuple[list[ColumnAt], WalkStop | None] | None:
    if column in stage.aggregate.group_by:
        return [ColumnAt(source_id, column)], None
    for operation in stage.aggregate.aggregations:
        if operation.output_column != column:
            continue
        if operation.value_column is None:
            return [], WalkStop.counts_rows
        return [ColumnAt(source_id, operation.value_column)], None
    return None


def _find_landed_parent(
    stage: JoinStage, workflow_stage: WorkflowStage, column: str
) -> tuple[list[ColumnAt], WalkStop | None] | None:
    """The reference column `enrich_with` names, plus the keys that chose its row."""
    came_from = {landed: source for source, landed in stage.join.enrich_with.items()}
    if column not in came_from:
        return None
    subject, reference = workflow_stage.inputs[0].id, workflow_stage.inputs[1].id
    keys = [at for key in stage.join.keys
            for at in (ColumnAt(subject, key.left), ColumnAt(reference, key.right))]
    return [ColumnAt(reference, came_from[column]), *keys], None


def _find_carrying_inputs(workflow_stage: WorkflowStage, column: str) -> list[ColumnAt]:
    # A union stacks frames, so its rows arrive from every side.
    sides = (
        workflow_stage.inputs
        if workflow_stage.stage.type == StageType.union
        else workflow_stage.inputs[:1]
    )
    return [
        ColumnAt(side.id, column)
        for side in sides
        if side.table_schema.column_for_name(column) is not None
    ]


def _list_read_columns(stage: AbstractStage) -> list[ColumnAt]:
    return [
        ColumnAt(entry.input, column.name)
        for entry in stage.signature.reads
        for column in entry.columns
    ]


def _find_writers_upstream(
    stages: WorkflowStagesById,
    stage_id: StageId,
    writers: set[StageId],
    on_walk: set[StageId],
) -> list[StageId]:
    found: list[StageId] = []
    seen = {stage_id}
    frontier = deque(stages[stage_id].stage.input_ids)
    while frontier:
        upstream = frontier.popleft()
        if upstream in seen or upstream not in on_walk:
            continue
        seen.add(upstream)
        if upstream in writers:
            found.append(upstream)
        else:
            # Its own inputs stand in its place, keeping a union's sides visible.
            frontier.extend(stages[upstream].stage.input_ids)
    return found
