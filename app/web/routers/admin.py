"""Workspace admin page: load a packaged seed fixture, download a project as a
portable WorkflowFile document, or upload one back into the workspace.

Every path param is matched against a known list before use, so a request for an
unknown name 404s instead of reaching the seam with unsanitized input.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError

from app.core.errors import ProjectExistsError
from app.seeds.seed import discover_workflow_files
from app.services import project
from app.services.project import WorkflowFile, export_project, import_project
from app.web.config import templates

router = APIRouter()


# ─── Path guards ───────────────────────────────────────────────────────────
# Every {bundle}/{project_name} below is checked against a list the seam
# itself just enumerated (discover_workflow_files() / list_projects()) —
# never a filesystem path built directly from the request.

def _bundle_path(bundle: str) -> Path:
    """The packaged WorkflowFile json path named `bundle`, or a 404. Matches
    by stem against discover_workflow_files()'s own listing, so this can only
    ever return a path the seam already enumerated from disk — never one
    built from the unvalidated request string."""
    for candidate in discover_workflow_files():
        if candidate.stem == bundle:
            return candidate
    raise HTTPException(status_code=404, detail=f"No seed bundle '{bundle}'")


def _known_project(project_name: str) -> str:
    """`project_name`, or a 404 if it names no current project."""
    if project_name not in project.list_projects():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return project_name


def _redirect_to_admin(msg: str) -> RedirectResponse:
    """303 back to the admin page carrying a one-line status message."""
    return RedirectResponse(url=f"/admin?{urlencode({'msg': msg})}", status_code=303)


# ─── Page ────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, msg: str | None = None):
    """The packaged seed fixtures (available to load) and the workspace's
    current projects (available to export), plus the status message left by
    the last action, if any."""
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "bundles": [wf_path.stem for wf_path in discover_workflow_files()],
            "projects": project.list_projects(),
            "msg": msg,
        },
    )


# ─── Actions ───────────────────────────────────────────────────────────────

@router.post("/admin/load/{bundle}")
async def load_bundle(bundle: str):
    """Import a seed fixture if its project doesn't already exist. Import-if-
    absent only: an existing project of the same name is left exactly as it
    is, reported back rather than clobbered, so loading the same bundle twice
    is safe."""
    wf = WorkflowFile.model_validate_json(_bundle_path(bundle).read_text(encoding="utf-8"))
    try:
        name = import_project(wf)
    except ProjectExistsError:
        existing_name = project.sanitize_project_name(wf.name)
        return _redirect_to_admin(f"'{existing_name}' already exists — not loaded.")
    return _redirect_to_admin(f"Loaded '{name}' from bundle '{bundle}'.")


@router.get("/admin/export/{project_name}")
async def download_project(project_name: str) -> Response:
    """A project's WorkflowFile document as a `<project>.json` browser download."""
    name = _known_project(project_name)
    return Response(
        content=export_project(name).to_json(),
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{name}.json"'},
    )


@router.post("/admin/import")
async def upload_project(file: UploadFile = File(...)):
    """Import an uploaded WorkflowFile. Nothing is written until it validates."""
    wf = _parse_workflow_file(await file.read(), file.filename)
    try:
        name = import_project(wf)
    except ProjectExistsError:
        existing_name = project.sanitize_project_name(wf.name)
        return _redirect_to_admin(f"'{existing_name}' already exists — not imported.")
    return _redirect_to_admin(f"Imported '{name}' from an uploaded file.")


def _parse_workflow_file(raw: bytes, filename: str | None) -> WorkflowFile:
    """The whole upload as a WorkflowFile, or a 400 — no project is written on the way."""
    try:
        return WorkflowFile.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a valid WorkflowFile document: {exc}",
        ) from exc
