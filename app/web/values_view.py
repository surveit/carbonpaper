"""The walk turned into sheets, each read off this run's own frames."""

from __future__ import annotations

from collections import defaultdict

from app.core.errors import ColumnNotInFrame, StageNotInRun
from app.models.schema import StageId
from app.services import run as run_service
from app.services.scope import find_rows_reached_per_stage
from app.web.diagrams import TYPE_GLYPH
from app.web.loading import load_run_record
from app.web.run_stage_view import TraceScope
from app.web.scope_view import read_run_branches
from app.web.values_payload import (
    MinimapEdge,
    MinimapNode,
    StepSource,
    ValuesUsed,
)
from app.web.column_walk import (
    ColumnAt,
    ColumnWalk,
    WalkStop,
    WorkflowStagesById,
    WriterGraph,
    build_writer_graph,
    find_nearest_writer_upstream,
    walk_column_back,
)


def load_values_used(
    project_id: str, run_id: str, stage_id: StageId, column: str, row: int
) -> ValuesUsed:
    record = load_run_record(project_id, run_id)
    stages = _read_run_stages(project_id, run_id, record.to_dict(), stage_id, column)
    walk = walk_column_back(stages, ColumnAt(stage_id, column))
    graph = build_writer_graph(walk, stages)
    level = _rank_stages_by_graph_level(graph, _list_stages_that_wrote(walk))
    replayed = sorted(level, key=lambda sid: (level[sid], sid))
    return ValuesUsed(
        cited_stage=stage_id,
        column=column,
        row=row,
        steps=replayed,
        minimap=_build_minimap(level, replayed, stages),
        edges=_list_edges(graph),
        sources=_index_sources(graph),
        counts_rows=walk.find_stop_at(ColumnAt(stage_id, column)) is WalkStop.counts_rows,
    )


def build_trace_scope(
    project_id: str, run_id: str, stage_id: StageId, column: str, row: int
) -> TraceScope:
    """What cuts a stage panel down to one figure: its rows, and where each column began."""
    record = load_run_record(project_id, run_id)
    stages = _read_run_stages(project_id, run_id, record.to_dict(), stage_id, column)
    walk = walk_column_back(stages, ColumnAt(stage_id, column))
    graph = build_writer_graph(walk, stages)
    reached = find_rows_reached_per_stage(
        read_run_branches(project_id, run_id), [(stage_id, row)])
    return TraceScope(
        cited_column=column,
        rows_by_stage={sid: sorted(rows) for sid, rows in reached.items()},
        column_writers={
            name: writer
            for name in {at.column for at in walk.nodes}
            for writer in [find_nearest_writer_upstream(walk, graph, stage_id, name)]
            if writer is not None
        },
    )


def _read_run_stages(
    project_id: str, run_id: str, manifest: dict, stage_id: StageId, column: str
) -> WorkflowStagesById:
    stages = run_service.load_run_workflow(
        project_id, manifest).index_workflow_stages_by_id()
    workflow_stage = stages.get(stage_id)
    if workflow_stage is None:
        raise StageNotInRun(f"no stage '{stage_id}' in run '{run_id}'")
    schema = workflow_stage.output_schema
    if schema is None or schema.column_for_name(column) is None:
        raise ColumnNotInFrame(f"stage '{stage_id}' writes no column '{column}'")
    return stages


def _list_stages_that_wrote(walk: ColumnWalk) -> set[StageId]:
    return {at.stage_id for at, node in walk.nodes.items() if node.wrote}


def _rank_stages_by_graph_level(
    graph: WriterGraph, wrote: set[StageId]
) -> dict[StageId, int]:
    """One column right of its furthest parent, so two sources of one stage stack."""
    level: dict[StageId, int] = {}

    def measure(stage_id: StageId) -> int:
        if stage_id not in level:
            parents = [edge.from_stage for edge in graph.list_parents(stage_id)]
            level[stage_id] = 0 if not parents else 1 + max(map(measure, parents))
        return level[stage_id]

    for stage_id in sorted(wrote):
        measure(stage_id)
    return level


def _build_minimap(
    level: dict[StageId, int], replayed: list[StageId], stages: WorkflowStagesById
) -> list[list[MinimapNode]]:
    by_level: dict[int, list[StageId]] = defaultdict(list)
    for stage_id in replayed:
        by_level[level[stage_id]].append(stage_id)
    return [
        [
            MinimapNode(stage_id=sid, glyph=TYPE_GLYPH[stages[sid].stage.type])
            for sid in by_level[column]
        ]
        for column in sorted(by_level)
    ]


def _list_edges(graph: WriterGraph) -> list[MinimapEdge]:
    return [
        MinimapEdge(from_stage=edge.from_stage, to_stage=child, columns=len(edge.columns))
        for child, edges in graph.parents.items()
        for edge in edges
    ]


def _index_sources(graph: WriterGraph) -> dict[StageId, list[StepSource]]:
    return {
        child: [
            StepSource(stage_id=edge.from_stage, columns=list(edge.columns))
            for edge in edges
        ]
        for child, edges in graph.parents.items()
    }
