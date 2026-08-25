"""Its own router because app.web.routers.runs is at the import fan-out ceiling."""

from __future__ import annotations

import csv
import io

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from app.core.errors import (
    ColumnNotInFrame,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.core.frames import read_frame_table
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.runtime.errors import MissingLineage
from app.services.input_check import compare_slice_to_the_file
from app.services.workspace import resolve_run_dir
from app.web import input_files_view
from app.web.config import templates
from app.web.input_files_view import Basis, InputFileSlice, InputFilesView

router = APIRouter()

_BASE = "/project/{project_id}/runs/{run_id}/input-files"
_UNREADABLE = (MissingLineage, StageNotInRun, RowOutOfRange, ColumnNotInFrame,
               RunVersionUnresolvableError)


@router.get(f"{_BASE}/panel", response_class=HTMLResponse)
async def input_files_panel(request: Request, project_id: str, run_id: str,
                            stage: str, row: int, column: str):
    """The files this figure read, shell-less: the row lineage page holds it in a tab."""
    try:
        view = _load(project_id, run_id, stage, row, column)
    except _UNREADABLE as no_files:
        # A pane that 404s shows the reader a browser error page inside a tab.
        return templates.TemplateResponse(
            request, "_input_files_panel.html",
            {"project": project_id, "run_id": run_id, "stage_id": stage,
             "column": column, "reason": str(no_files)})
    return templates.TemplateResponse(
        request, "_input_files_panel.html",
        {"project": project_id, "run_id": run_id, "stage_id": stage,
         "column": column, "view": view})


@router.get(f"{_BASE}/check.json", response_class=JSONResponse)
async def input_file_check(project_id: str, run_id: str, stage: str, row: int,
                           column: str, input: str, rows: Basis = Basis.relevant,
                           columns: Basis = Basis.relevant):
    """Re-reads the file and compares the slice — a click, never a page load."""
    slice_ = _find_file(_load(project_id, run_id, stage, row, column), input)
    compared = compare_slice_to_the_file(
        project_id, run_id, slice_.stage_id, _pick_rows(slice_, rows),
        _pick_columns(slice_, columns))
    return JSONResponse(compared.model_dump(mode="json"))


@router.get(f"{_BASE}/slice.csv")
async def input_file_slice(project_id: str, run_id: str, stage: str, row: int,
                           column: str, input: str, rows: Basis = Basis.relevant,
                           columns: Basis = Basis.relevant):
    slice_ = _find_file(_load(project_id, run_id, stage, row, column), input)
    wanted = _pick_columns(slice_, columns)
    frame = read_frame_table(
        resolve_run_dir(project_id, run_id) / "outputs" / f"{slice_.stage_id}.parquet")
    return StreamingResponse(
        io.StringIO(_render_as_csv(frame, _pick_rows(slice_, rows), wanted)),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{slice_.stage_id}-slice.csv"'})


def _load(project_id: str, run_id: str, stage: str, row: int,
          column: str) -> InputFilesView:
    return input_files_view.load_input_files(project_id, run_id, StageOutputCellCitation(
        run_id=run_id, stage_id=stage, row_ordinal=row, column=column, value=None))


def _find_file(view: InputFilesView, stage_id: StageId) -> InputFileSlice:
    for one in view.files:
        if one.stage_id == stage_id:
            return one
    raise HTTPException(status_code=404,
                        detail=f"this figure read no file at '{stage_id}'")


def _pick_rows(slice_: InputFileSlice, rows: Basis) -> list[int]:
    if rows is Basis.all:
        return list(range(slice_.rows_read))
    return list(slice_.ordinals)


def _pick_columns(slice_: InputFileSlice, columns: Basis) -> list[str]:
    if columns is Basis.all:
        return slice_.columns_read
    return slice_.columns_relevant


def _render_as_csv(frame, ordinals: list[int], columns: list[str]) -> str:
    sheet = io.StringIO()
    writer = csv.writer(sheet)
    writer.writerow(columns)
    picked = {name: frame.column(name) for name in columns}
    for ordinal in ordinals:
        writer.writerow([picked[name][ordinal].as_py() for name in columns])
    return sheet.getvalue()
