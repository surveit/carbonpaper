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

from app.seeds.seed import discover_workflow_files
from app.services import project
from app.services.project import (
    AdminProjectListing, WorkflowFile, read_project_name, export_project, import_project,
)
from app.web.config import templates

router = APIRouter()


# ─── Path guards ───────────────────────────────────────────────────────────
# Every {bundle}/{project_name} below is checked against a list the seam itself
# just enumerated — never a filesystem path built directly from the request.

def _bundle_path(bundle: str) -> Path:
    for candidate in discover_workflow_files():
        if candidate.stem == bundle:
            return candidate
    raise HTTPException(status_code=404, detail=f"No seed bundle '{bundle}'")


def _known_project(project_name: str) -> str:
    if project_name not in [row.id for row in _list_projects()]:
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return project_name


def _list_projects() -> list[AdminProjectListing]:
    """Admin lists a private project, so it is where the operator finds one again."""
    return project.list_project_listings_including_private()


def _redirect_to_admin(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin?{urlencode({'msg': msg})}", status_code=303)


# ─── Page ────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, msg: str | None = None):
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "bundles": [wf_path.stem for wf_path in discover_workflow_files()],
            "projects": _list_projects(),
            "msg": msg,
        },
    )


# ─── Actions ───────────────────────────────────────────────────────────────

@router.post("/admin/load/{bundle}")
async def load_bundle(bundle: str):
    wf = WorkflowFile.model_validate_json(_bundle_path(bundle).read_text(encoding="utf-8"))
    # Loading a bundle twice makes a SECOND project rather than being refused: a label
    # is not unique, so there is nothing to clash with and nothing to overwrite. The
    # message names the id, which is the only half that tells the two of them apart.
    project_id = import_project(wf)
    return _redirect_to_admin(
        f"Loaded '{read_project_name(project_id)}' ({project_id}) from bundle '{bundle}'."
    )


@router.get("/admin/export/{project_name}")
async def download_project(project_name: str) -> Response:
    project_id = _known_project(project_name)
    bundle = export_project(project_id)
    # The bundle's own name, which is the slug — a shown name may carry spaces and dashes.
    return Response(
        content=bundle.to_json(),
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{bundle.name}.json"'},
    )


@router.post("/admin/import")
async def upload_project(file: UploadFile = File(...)):
    wf = _parse_workflow_file(await file.read(), file.filename)
    project_id = import_project(wf)
    return _redirect_to_admin(
        f"Imported '{read_project_name(project_id)}' ({project_id}) from an uploaded file."
    )


def _parse_workflow_file(raw: bytes, filename: str | None) -> WorkflowFile:
    try:
        return WorkflowFile.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a valid WorkflowFile document: {exc}",
        ) from exc
