"""The project's Files page, and the delete behind it. Uploading is
app.web.routers.run_form, which owns the endpoint both the run form and curl post to."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.errors import FileNotStoredError
from app.services.project import project_exists
from app.services.uploads import delete_file
from app.services.workspace import resolve_project_dir
from app.web.config import templates
from app.web.files_view import build_files_view
from app.web.project_view import shell_state

router = APIRouter()


@router.get("/project/{project}/files", response_class=HTMLResponse)
async def files_page(request: Request, project: str):
    if not project_exists(project):
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return templates.TemplateResponse(
        request,
        "section_files.html",
        {
            "state": shell_state(resolve_project_dir(project), "files"),
            "section": "files",
            "files": build_files_view(project),
        },
    )


@router.post("/project/{project}/files/{sha256}/delete")
async def delete_project_file(project: str, sha256: str, confirm: str = Form("")):
    """Deleting is the one thing here that cannot be undone, so it takes a POST and a
    typed confirmation."""
    if not project_exists(project):
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # The filename, typed. A run that read this file keeps its manifest either way, but
    # re-running it stops working the moment the bytes go — so this asks for a deliberate
    # act rather than a click that a mis-aimed cursor can make.
    if confirm.strip() != _filename_of(project, sha256):
        raise HTTPException(status_code=400,
                            detail="type the file's name to confirm the delete")
    try:
        delete_file(project, sha256)
    except FileNotStoredError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(url=f"/project/{project}/files", status_code=303)


def _filename_of(project: str, sha256: str) -> str:
    row = next((r for r in build_files_view(project).rows if r.sha256 == sha256), None)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no file {sha256!r} in this project")
    return row.filename
