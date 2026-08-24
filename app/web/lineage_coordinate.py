"""What the row-lineage header names, and the stages, rows and columns it offers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.models import WorkflowStage
from app.models.records.run_manifest import RunManifest
from app.web.config import label_stage_type, render_row_number
from app.web.trace_row_diff import render_cell


class StagePick(BaseModel):
    stage_id: str
    type_label: str
    rows: int


class CitedCell(BaseModel):
    column: str
    value: str
    # The authored description, or a plain statement of why there is none.
    tip: str


class LineageCoordinate(BaseModel):
    stages: list[StagePick]
    stage_id: str
    # The ordinal the address carries, and the number the box shows.
    row: int
    row_number: str
    # None where this run recorded no frame for the stage: nothing counted its rows.
    rows: int | None
    # Empty where the walk read no row, which is a page with no columns to offer.
    cells: list[CitedCell]
    column: str | None

    @property
    def cell(self) -> CitedCell | None:
        return next((c for c in self.cells if c.column == self.column), None)


def build_lineage_coordinate(
    manifest: RunManifest,
    view: dict[str, Any],
    workflow_stage: WorkflowStage | None,
    column: str | None,
) -> LineageCoordinate:
    stages = _list_stages_with_a_frame(manifest)
    stage_id = view["start_stage"]
    return LineageCoordinate(
        stages=stages,
        stage_id=stage_id,
        row=view["start_row"],
        row_number=render_row_number(view["start_row"]),
        rows=next((s.rows for s in stages if s.stage_id == stage_id), None),
        cells=_describe_row_cells(view, workflow_stage),
        column=column,
    )


def _list_stages_with_a_frame(manifest: RunManifest) -> list[StagePick]:
    """An interrupted run leaves records for stages it never reached — no frame, no row to trace."""
    return [
        StagePick(stage_id=record.stage_id,
                  type_label=label_stage_type(record.type),
                  rows=record.output_row_count)
        for record in manifest.stage_records
        if record.output_path and record.output_row_count > 0
    ]


def _describe_row_cells(
    view: dict[str, Any], workflow_stage: WorkflowStage | None
) -> list[CitedCell]:
    if not view["nodes"]:
        return []
    row = view["nodes"][-1]["row"]
    return [
        CitedCell(column=name, value=render_cell(value),
                  tip=_describe_column(view["start_stage"], workflow_stage, name))
        for name, value in row.items()
    ]


def _describe_column(
    stage_id: str, workflow_stage: WorkflowStage | None, column: str
) -> str:
    if workflow_stage is None:
        return f"The version this run pinned is unreadable, so nothing declares {column} here."
    schema = workflow_stage.output_schema
    declared = schema.column_for_name(column) if schema else None
    if declared is None:
        return f"The version this run pinned declares no {column} on {stage_id}."
    if declared.description:
        return declared.description
    nullability = "null allowed" if declared.nullable else "not null"
    return (f"Declared {declared.type}, {nullability}. "
            f"No description was authored for this column.")
