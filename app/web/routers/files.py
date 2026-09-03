"""The project's Files page, and the delete behind it. Uploading is
app.web.routers.run_form, which owns the endpoint both the run form and curl post to."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.core.errors import FileNotStoredError
from app.services.project import project_exists
from app.core.files import FileCompleteness, delete_file, update_file_provenance
from app.web.config import templates
from app.web.file_detail_view import build_file_detail_view
from app.web.file_preview import build_file_preview
from app.web.files_view import build_files_view
from app.web.project_view import shell_state

router = APIRouter()


@router.get("/project/{project_id}/files", response_class=HTMLResponse)
def files_page(request: Request, project_id: str):
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    return templates.TemplateResponse(
        request,
        "section_files.html",
        {
            "state": shell_state(project_id, "files"),
            "section": "files",
            "files": build_files_view(project_id),
        },
    )


@router.get("/project/{project_id}/files/{file_id}", response_class=HTMLResponse)
async def file_page(request: Request, project_id: str, file_id: str):
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    try:
        view = await run_in_threadpool(build_file_detail_view, project_id, file_id)
    except FileNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "file_detail.html",
        {"state": shell_state(project_id, "files"), "section": "files", "file": view},
    )


@router.get("/project/{project_id}/files/{file_id}/preview", response_class=HTMLResponse)
async def preview_project_file(request: Request, project_id: str, file_id: str):
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    try:
        preview = await run_in_threadpool(build_file_preview, project_id, file_id)
    except FileNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request, "_file_preview.html", {"preview": preview}
    )


@router.post("/project/{project_id}/files/{file_id}/delete")
def delete_project_file(project_id: str, file_id: str, confirm: str = Form("")):
    """Deleting is the one thing here that cannot be undone, so it takes a POST and a
    typed confirmation."""
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    # The filename, typed. A run that read this file keeps its manifest either way, but
    # re-running it stops working the moment the bytes go — so this asks for a deliberate
    # act rather than a click that a mis-aimed cursor can make.
    if confirm.strip() != _filename_of(project_id, file_id):
        raise HTTPException(status_code=400,
                            detail="type the file's name to confirm the delete")
    try:
        delete_file(project_id, file_id)
    except FileNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{project_id}/files", status_code=303)


@router.post("/project/{project_id}/files/{file_id}/provenance")
def record_file_provenance(project_id: str, file_id: str,
                                 completeness: str = Form(...), lineage: str = Form("")):
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    try:
        claim = FileCompleteness(completeness)
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail=f"invalid completeness {completeness!r}") from exc
    try:
        update_file_provenance(project_id, file_id, claim, lineage)
    except FileNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{project_id}/files/{file_id}", status_code=303)


def _filename_of(project_id: str, file_id: str) -> str:
    row = next((r for r in build_files_view(project_id).rows if r.file_id == file_id), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no file {file_id!r} in this project")
    return row.filename
