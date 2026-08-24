"""The walk turned into sheets, each read off this run's own frames."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.errors import ColumnNotInFrame, StageNotInRun
from app.models import AbstractStage, StageType, WorkflowStage
from app.models.run_manifest import StageRecord
from app.models.schema import StageId
from app.models.stages.aggregate import AggregateStage
from app.models.stages.signature import ExtendsSignature
from app.services import run as run_service
from app.services.workspace import resolve_run_dir
from app.web.diagrams import TYPE_GLYPH, TYPE_LABEL
from app.web.diff_state import ColumnDiffState
from app.web.loading import load_output_preview, load_run_record
from app.web.values_payload import (
    MinimapEdge,
    MinimapNode,
    NewSheet,
    SheetColumn,
    StepSource,
    ValuesStep,
    ValuesUsed,
)
from app.web.values_walk import (
    ColumnAt,
    ColumnWalk,
    WalkStop,
    WorkflowStagesById,
    WriterGraph,
    build_writer_graph,
    find_nearest_writer_upstream,
    order_sheet_columns,
    walk_column_back,
)

# A step has to sit on one screen beside the transform that made it.
SHEET_ROWS_SHOWN = 6


def load_values_used(
    project_id: str, run_id: str, stage_id: StageId, column: str, row: int
) -> ValuesUsed:
    record = load_run_record(project_id, run_id)
    stages = run_service.load_run_workflow(
        project_id, record.to_dict()).index_workflow_stages_by_id()
    _refuse_unknown_cell(stages, run_id, stage_id, column)
    walk = walk_column_back(stages, ColumnAt(stage_id, column))
    graph = build_writer_graph(walk, stages)
    replayed = _order_stages_that_wrote(walk)
    records = {entry.stage_id: entry for entry in record.stage_records}
    run_dir = resolve_run_dir(project_id, run_id)
    order = order_sheet_columns(walk)
    return ValuesUsed(
        cited_stage=stage_id,
        column=column,
        row=row,
        steps=[
            _build_step(stages[sid], records.get(sid), run_dir, walk, graph, order,
                        cited=ColumnAt(stage_id, column))
            for sid in replayed
        ],
        minimap=_build_minimap(walk, replayed, stages),
        edges=_list_edges(graph),
        sources=_index_sources(graph),
        counts_rows=walk.find_stop_at(ColumnAt(stage_id, column)) is WalkStop.counts_rows,
    )


def _refuse_unknown_cell(
    stages: WorkflowStagesById, run_id: str, stage_id: StageId, column: str
) -> None:
    workflow_stage = stages.get(stage_id)
    if workflow_stage is None:
        raise StageNotInRun(f"no stage '{stage_id}' in run '{run_id}'")
    schema = workflow_stage.output_schema
    if schema is None or schema.column_for_name(column) is None:
        raise ColumnNotInFrame(f"stage '{stage_id}' writes no column '{column}'")


def _order_stages_that_wrote(walk: ColumnWalk) -> list[StageId]:
    """Root first, so the replay reads left to right like the workflow graph."""
    wrote = {at.stage_id for at, node in walk.nodes.items() if node.wrote}
    return sorted(wrote, key=lambda sid: (-walk.measure_depth_of_stage(sid), sid))


def _build_step(
    workflow_stage: WorkflowStage,
    record: StageRecord | None,
    run_dir: Path,
    walk: ColumnWalk,
    graph: WriterGraph,
    order: list[str],
    cited: ColumnAt,
) -> ValuesStep:
    stage = workflow_stage.stage
    preview = None if record is None else load_output_preview(
        run_dir, record.output_path, SHEET_ROWS_SHOWN)
    unreadable = _say_why_no_sheet(record, preview)
    on_frame = set() if preview is None else {str(name) for name in preview["columns"]}
    new_sheet = _find_new_sheet(stage)
    present = _resolve_present_columns(order, on_frame, new_sheet, unreadable)
    states = _SheetStates(
        on_frame=on_frame,
        rewritten=_list_rewritten_columns(stage),
        read_here=_list_columns_read_here(walk, stage.id),
    )
    return ValuesStep(
        stage_id=stage.id,
        glyph=TYPE_GLYPH[stage.type],
        label=TYPE_LABEL[stage.type],
        rows_total=0 if record is None else record.output_row_count,
        new_sheet=new_sheet,
        columns=[
            _build_sheet_column(name, stage.id, walk, graph, states, cited)
            for name in present
        ],
        rows=[] if preview is None else [
            [str(row.get(name, "")) for name in present] for row in preview["preview"]
        ],
        columns_total=len(on_frame),
        unreadable=unreadable,
    )


def _resolve_present_columns(
    order: list[str], on_frame: set[str], new_sheet: NewSheet | None, unreadable: str | None
) -> list[str]:
    if unreadable is not None:
        return []
    # A rebuilt frame keeps none of the old columns, so no slot is held.
    if new_sheet is not None:
        return [name for name in order if name in on_frame]
    return list(order)


@dataclass(frozen=True)
class _SheetStates:
    """What one stage did to each column, taken off the stage and the walk once."""

    on_frame: set[str]
    rewritten: set[str]
    read_here: set[str]


def _build_sheet_column(
    name: str,
    stage_id: StageId,
    walk: ColumnWalk,
    graph: WriterGraph,
    states: _SheetStates,
    cited: ColumnAt,
) -> SheetColumn:
    node = walk.nodes.get(ColumnAt(stage_id, name))
    return SheetColumn(
        name=name,
        state=_resolve_column_state(name, states, wrote=node is not None and node.wrote),
        cited=ColumnAt(stage_id, name) == cited,
        writer=find_nearest_writer_upstream(walk, graph, stage_id, name),
    )


def _resolve_column_state(name: str, states: _SheetStates, *, wrote: bool) -> ColumnDiffState:
    if name not in states.on_frame:
        return ColumnDiffState.absent
    if name in states.rewritten:
        return ColumnDiffState.rewritten
    if wrote:
        return ColumnDiffState.added
    if name in states.read_here:
        return ColumnDiffState.read
    return ColumnDiffState.carried


def _list_rewritten_columns(stage: AbstractStage) -> set[str]:
    signature = stage.signature
    if not isinstance(signature, ExtendsSignature):
        return set()
    return {column.name for column in signature.rewrites}


def _list_columns_read_here(walk: ColumnWalk, stage_id: StageId) -> set[str]:
    """Named by the parents of what this stage wrote, so it is the walk's own read set."""
    return {
        parent.column
        for at, node in walk.nodes.items()
        if at.stage_id == stage_id and node.wrote
        for parent in node.parents
    }


def _find_new_sheet(stage: AbstractStage) -> NewSheet | None:
    if isinstance(stage.signature, ExtendsSignature) or stage.type == StageType.input_data:
        return None
    if isinstance(stage, AggregateStage):
        return NewSheet.per_group if stage.aggregate.group_by else NewSheet.one_row
    return NewSheet.rebuilt


def _say_why_no_sheet(
    record: StageRecord | None, preview: dict[str, Any] | None
) -> str | None:
    if record is None:
        return "this run has no record of the stage"
    if preview is None:
        return "the stage recorded no output frame"
    error = preview.get("error")
    return None if error is None else str(error)


def _build_minimap(
    walk: ColumnWalk, replayed: list[StageId], stages: WorkflowStagesById
) -> list[list[MinimapNode]]:
    by_depth: dict[int, list[StageId]] = defaultdict(list)
    for stage_id in replayed:
        by_depth[walk.measure_depth_of_stage(stage_id)].append(stage_id)
    return [
        [
            MinimapNode(stage_id=sid, glyph=TYPE_GLYPH[stages[sid].stage.type])
            for sid in by_depth[depth]
        ]
        for depth in sorted(by_depth, reverse=True)
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
