"""Its own router because app.web.routers.runs is at the import fan-out ceiling."""

from __future__ import annotations

import csv
import io
from pathlib import PurePath

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.core.errors import (
    ColumnNotInFrame,
    MissingInputBindingError,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.core.frames import read_frame_table, read_native_scalar_as_json
from app.models.citations import StageOutputCellCitation
from app.runtime.errors import MissingLineage
from app.services.workspace import resolve_run_dir
from app.web import input_files_view
from app.web.config import templates
from app.web.input_files_view import Basis, InputFileSlice, InputFilesView
from app.models.run_manifest import RunKind

router = APIRouter()

_BASE = "/project/{project_id}/runs/{run_id}/input-files"
_UNREADABLE = (MissingLineage, StageNotInRun, RowOutOfRange, ColumnNotInFrame,
               RunVersionUnresolvableError, MissingInputBindingError)


@router.get(f"{_BASE}/panel", response_class=HTMLResponse)
def input_files_panel(request: Request, project_id: str, run_id: str,
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


@router.get(f"{_BASE}/slice.csv")
def input_file_slice(project_id: str, run_id: str, stage: str, row: int,
                           column: str, file: int, rows: Basis = Basis.relevant,
                           columns: Basis = Basis.relevant):
    slice_ = _find_file(_load(project_id, run_id, stage, row, column), file)
    wanted = _choose_columns(slice_, columns)
    frame = read_frame_table(
        resolve_run_dir(project_id, run_id, RunKind.production) / "outputs" / f"{slice_.stage_id}.parquet")
    return StreamingResponse(
        io.StringIO(_render_as_csv(frame, _choose_rows(slice_, rows), wanted)),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{PurePath(slice_.filename).stem}-slice.csv"'})


def _load(project_id: str, run_id: str, stage: str, row: int,
          column: str) -> InputFilesView:
    return input_files_view.load_input_files(project_id, run_id, StageOutputCellCitation(
        run_id=run_id, stage_id=stage, row_ordinal=row, column=column, value=None))


def _find_file(view: InputFilesView, file: int) -> InputFileSlice:
    """`file` is the file's place in `view.files`, as the panel listed them."""
    if 0 <= file < len(view.files):
        return view.files[file]
    raise HTTPException(status_code=404,
                        detail=f"no file {file} among the {len(view.files)} this figure read")


def _choose_rows(slice_: InputFileSlice, rows: Basis) -> list[int]:
    if rows is Basis.all:
        return list(slice_.ordinals_read)
    return list(slice_.ordinals_relevant)


def _choose_columns(slice_: InputFileSlice, columns: Basis) -> list[str]:
    if columns is Basis.all:
        return slice_.columns_read
    return slice_.columns_relevant


def _render_as_csv(frame, ordinals: list[int], columns: list[str]) -> str:
    sheet = io.StringIO()
    writer = csv.writer(sheet)
    writer.writerow(columns)
    picked = {name: frame.column(name) for name in columns}
    for ordinal in ordinals:
        writer.writerow([read_native_scalar_as_json(picked[name][ordinal]) for name in columns])
    return sheet.getvalue()
