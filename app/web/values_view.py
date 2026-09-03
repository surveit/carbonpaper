"""The walk over the run's graph, and the scope it cuts each stage's panel down to."""

from __future__ import annotations

from app.core.errors import ColumnNotInFrame, StageNotInRun
from app.models.schema import StageId
from app.services import run as run_service
from app.services.scope import find_rows_reached_per_stage
from app.web.diagrams import TYPE_GLYPH, build_mermaid_graph
from app.web.walk_diagram import build_walk_overlay, read_walk_state
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
    parents = _index_parents(stages)
    level = _rank_stages_by_graph_level(parents)
    on_walk = _list_stages_on_the_walk(walk)
    behind = _count_rows_behind(project_id, run_id, stage_id, row, stages)
    edges = _list_edges(parents, on_walk, behind)
    return ValuesUsed(
        cited_stage=stage_id,
        column=column,
        row=row,
        steps=sorted(on_walk, key=lambda sid: (level[sid], sid)),
        mermaid=_draw_walk_graph(stages, on_walk, behind, edges),
        nodes=_list_nodes(stages, on_walk, behind),
        edges=edges,
        sources=_index_sources(parents, behind),
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


def _count_rows_behind(
    project_id: str, run_id: str, stage_id: StageId, row: int,
    stages: WorkflowStagesById,
) -> dict[StageId, int]:
    reached = find_rows_reached_per_stage(
        read_run_branches(project_id, run_id), [(stage_id, row)])
    return {sid: len(reached.get(sid, ())) for sid in stages}


def _list_stages_on_the_walk(walk: ColumnWalk) -> set[StageId]:
    return {at.stage_id for at in walk.nodes}


def _index_parents(stages: WorkflowStagesById) -> dict[StageId, list[StageId]]:
    return {sid: list(placed.stage.input_ids) for sid, placed in stages.items()}


def _rank_stages_by_graph_level(
    parents: dict[StageId, list[StageId]]
) -> dict[StageId, int]:
    """One column right of its furthest parent, so two sources of one stage stack."""
    level: dict[StageId, int] = {}

    def measure(stage_id: StageId) -> int:
        if stage_id not in level:
            behind = [sid for sid in parents.get(stage_id, ()) if sid in parents]
            level[stage_id] = 0 if not behind else 1 + max(map(measure, behind))
        return level[stage_id]

    for stage_id in sorted(parents):
        measure(stage_id)
    return level


def _list_nodes(
    stages: WorkflowStagesById, on_walk: set[StageId], behind: dict[StageId, int]
) -> list[MinimapNode]:
    return [
        MinimapNode(
            stage_id=sid,
            glyph=TYPE_GLYPH[stages[sid].stage.type],
            on_walk=sid in on_walk,
            rows_behind=behind[sid],
        )
        for sid in sorted(stages)
    ]


def _draw_walk_graph(
    stages: WorkflowStagesById,
    on_walk: set[StageId],
    behind: dict[StageId, int],
    edges: list[MinimapEdge],
) -> str:
    states = {sid: read_walk_state(sid in on_walk, behind[sid]) for sid in stages}
    overlay = build_walk_overlay(
        states, behind,
        {(edge.from_stage, edge.to_stage): edge.rows for edge in edges})
    return build_mermaid_graph(
        [stages[sid].stage for sid in stages], project_id="", overlay=overlay)


def _list_edges(
    parents: dict[StageId, list[StageId]],
    on_walk: set[StageId],
    behind: dict[StageId, int],
) -> list[MinimapEdge]:
    return [
        MinimapEdge(
            from_stage=parent,
            to_stage=child,
            rows=behind[parent] if parent in on_walk else None,
        )
        for child in sorted(parents)
        for parent in parents[child]
        if parent in parents
    ]


def _index_sources(
    parents: dict[StageId, list[StageId]], behind: dict[StageId, int]
) -> dict[StageId, list[StepSource]]:
    return {
        child: [
            StepSource(stage_id=parent, rows=behind[parent])
            for parent in parents[child]
            if parent in parents
        ]
        for child in sorted(parents)
        if parents[child]
    }
