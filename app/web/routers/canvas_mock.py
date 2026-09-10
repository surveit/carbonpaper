"""A MOCK, not a feature: the figure's run as a canvas of stages and their sheets."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.frames import read_frame_table
from app.models.claims import StageOutputCellCitation
from app.services import run as run_service
from app.services.scope import find_rows_reached_per_stage
from app.services.workspace import resolve_run_dir
from app.web import values_view
from app.web.config import templates
from app.web.loading import load_run_record
from app.web.scope_view import read_run_branches
from app.web.stage_diff import FILTER_ROWS_KIND, build_stage_diff

router = APIRouter()

PREVIEW_ROWS = 8


@router.get("/project/{project_id}/runs/{run_id}/canvas", response_class=HTMLResponse)
def canvas_mock(request: Request, project_id: str, run_id: str,
                stage: str, row: int, column: str):
    values = values_view.load_values_used(project_id, run_id, stage, column, row)
    run_branches = read_run_branches(project_id, run_id)
    reached = find_rows_reached_per_stage(run_branches, [(stage, row)])
    record = load_run_record(project_id, run_id)
    workflow = run_service.load_run_workflow(
        project_id, record.to_dict()).index_workflow_stages_by_id()
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    stages = {}
    for sid in run_branches.ordered_stage_ids:
        frame = outputs / f"{sid}.parquet"
        total, columns, rows, rel_rows = 0, [], [], []
        if frame.exists():
            table = read_frame_table(frame)
            total, columns = table.num_rows, list(table.column_names)
            ordinals = sorted(reached.get(sid, ()))[:PREVIEW_ROWS]
            rel_rows = [[o] + [str(table.column(c)[o].as_py())[:60] for c in columns]
                        for o in ordinals]
            rest = [o for o in range(min(total, PREVIEW_ROWS)) if o not in ordinals]
            rows = [[o] + [str(table.column(c)[o].as_py())[:60] for c in columns]
                    for o in rest[:PREVIEW_ROWS - len(rel_rows)]]
        authored = workflow[sid].stage
        stages[sid] = {
            "type": str(authored.type), "description": authored.description,
            "inputs": list(authored.input_ids), "total": total,
            "rel": len(reached.get(sid, ())), "columns": columns,
            "rel_rows": rel_rows, "rows": rows,
            "filter_rows": _read_filter_window(project_id, run_id, workflow[sid],
                                               outputs, reached.get(sid, ())),
        }
    cell = StageOutputCellCitation(run_id=run_id, stage_id=stage, row_ordinal=row,
                                   column=column, value=None)
    payload = {
        "project": project_id, "run": run_id,
        "order": list(run_branches.ordered_stage_ids), "stages": stages,
        "steps": values.steps,
        "arms": {sid: [arm.model_dump() for arm in arms] for sid, arms in values.arms.items()},
        "cuts": [cut.model_dump() for cut in values.cuts],
        "edges": [edge.model_dump() for edge in values.edges],
        "cited": {"stage": stage, "column": column, "row": row,
                  "value": _read_cell(outputs, cell)},
    }
    return templates.TemplateResponse(request, "canvas_mock.html", {"payload": payload})


def _read_filter_window(project_id, run_id, workflow_stage, outputs, mine):
    """A filter's first input rows, kept and dropped in place, as its Data tab draws them."""
    output_by_id = {sid: str(outputs / f"{sid}.parquet") for sid in workflow_stage.stage.input_ids}
    diff = build_stage_diff(workflow_stage, outputs.parent, str(outputs / f"{workflow_stage.stage.id}.parquet"),
                            output_by_id, rows_shown=PREVIEW_ROWS)
    if diff is None or diff.kind != FILTER_ROWS_KIND:
        return None
    return [{"dropped": row.dropped, "mine": row.output_ordinal in set(mine),
             "cells": [str(c)[:60] for c in row.cells]} for row in diff.rows]


def _read_cell(outputs, cell: StageOutputCellCitation) -> str:
    table = read_frame_table(outputs / f"{cell.stage_id}.parquet")
    return str(table.column(cell.column)[cell.row_ordinal].as_py())
