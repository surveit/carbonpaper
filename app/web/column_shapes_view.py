"""The Column shapes tab: what each column holds, grouped by what the stage did to it."""

from __future__ import annotations

from typing import Sequence

from pydantic import BaseModel

from app.core.errors import StageNotInRun
from app.core.file_shape import VALUES_KEPT
from app.models import WorkflowStage
from app.models.records.run_manifest import RunManifest
from app.models.schema import STR_COLUMN_TYPE, StageId, TableSchema
from app.services import run as run_service
from app.services.frame_profile import measure_stage_rows_shape
from app.services.scope import find_rows_reached_per_stage
from app.web.column_order import (
    ColumnGroup,
    group_columns_by_signature,
    order_columns_by_group,
)
from app.web.file_detail_view import ColumnRow, build_column_row
from app.web.loading import load_run_record
from app.web.scope_view import read_run_branches

# Above this many columns the untouched group arrives folded rather than in full.
UNTOUCHED_SHOWN = 6


class ShapeGroup(BaseModel):
    """`dropped_names` carries the group no output frame holds a column of."""

    group: ColumnGroup
    columns: list[ColumnRow]
    dropped_names: list[str]


class ColumnShapes(BaseModel):
    stage_id: StageId
    # None from the run page, where no figure narrows the frame.
    column: str | None
    rows_relevant: int
    rows_in_frame: int
    over_relevant_rows: list[ShapeGroup]
    over_every_row: list[ShapeGroup]

    @property
    def untouched_is_long(self) -> bool:
        return any(len(group.columns) > UNTOUCHED_SHOWN
                   for group in self.over_relevant_rows
                   if group.group is ColumnGroup.untouched)


def load_column_shapes(
    project_id: str, run_id: str, stage_id: StageId, *,
    cited_stage: StageId | None = None, cited_column: str | None = None,
    cited_row: int | None = None,
) -> ColumnShapes:
    """With no figure named, `relevant` is the whole frame and the toggle says so."""
    record = load_run_record(project_id, run_id)
    stages = run_service.load_run_workflow(
        project_id, record.to_dict()).index_workflow_stages_by_id()
    if stage_id not in stages:
        raise StageNotInRun(f"no stage '{stage_id}' in run '{run_id}'")
    placed, rows_in_frame = stages[stage_id], _count_rows_at(record, stage_id)
    ordinals = _read_rows_behind(project_id, run_id, stage_id, cited_stage, cited_row)
    over_every_row = _group_the_shape(
        project_id, run_id, stage_id, placed, range(rows_in_frame))
    return ColumnShapes(
        stage_id=stage_id,
        column=cited_column,
        rows_relevant=rows_in_frame if ordinals is None else len(ordinals),
        rows_in_frame=rows_in_frame,
        over_relevant_rows=over_every_row if ordinals is None else _group_the_shape(
            project_id, run_id, stage_id, placed, ordinals),
        over_every_row=over_every_row,
    )


def _read_rows_behind(
    project_id: str, run_id: str, stage_id: StageId,
    cited_stage: StageId | None, cited_row: int | None,
) -> list[int] | None:
    if cited_stage is None or cited_row is None:
        return None
    reached = find_rows_reached_per_stage(
        read_run_branches(project_id, run_id), [(cited_stage, cited_row)])
    return sorted(reached.get(stage_id, ()))


def _group_the_shape(
    project_id: str, run_id: str, at_stage: StageId, placed: WorkflowStage,
    ordinals: Sequence[int],
) -> list[ShapeGroup]:
    shape = measure_stage_rows_shape(
        project_id, run_id, at_stage, ordinals=list(ordinals), max_values=VALUES_KEPT,
        never_numbers=_list_text_columns(placed.output_schema))
    built = {column_shape.column: build_column_row(column_shape, shape.row_count)
             for column_shape in shape.columns}
    grouped = group_columns_by_signature(placed, list(built))
    dropped = _list_dropped_columns(placed, set(built))
    if dropped:
        grouped[ColumnGroup.dropped] = []
    return order_columns_by_group([
        ShapeGroup(group=group,
                   columns=[built[name] for name in names],
                   dropped_names=dropped if group is ColumnGroup.dropped else [])
        for group, names in grouped.items()
    ])


def _list_dropped_columns(placed: WorkflowStage, on_frame: set[str]) -> list[str]:
    """Named off the input schema, which is the only record of what is now missing."""
    if not placed.inputs:
        return []
    return sorted(column.name for column in placed.inputs[0].table_schema.columns
                  if column.name not in on_frame)


def _list_text_columns(schema: TableSchema | None) -> frozenset[str]:
    """A declared `str` keeps its text kind however its values happen to read."""
    if schema is None:
        return frozenset()
    return frozenset(column.name for column in schema.columns
                     if column.type == STR_COLUMN_TYPE)


def _count_rows_at(record: RunManifest, stage_id: StageId) -> int:
    return next((entry.output_row_count for entry in record.stage_records
                 if entry.stage_id == stage_id), 0)
