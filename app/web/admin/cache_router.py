"""Download one project's stage cache, and write one back into a project chosen here.
The destination is ASKED FOR, not read from the file: `import_project` mints a fresh
project id, so an export re-keyed onto the wrong one imports cleanly and is never read.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.services import project
from app.services.stage_cache_transfer import (
    CacheArchiveRejected, count_cached_entries, export_stage_cache, import_stage_cache,
)
from app.web.breadcrumbs import build_home_crumbs
from app.web.config import templates

router = APIRouter()

PAGE_TITLE = "Stage cache"


class ProjectCacheSize(BaseModel):
    id: str
    entry_count: int


@router.get("/admin/cache", response_class=HTMLResponse)
def cache_page(request: Request, msg: str | None = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_cache.html",
        {
            "projects": [
                ProjectCacheSize(id=p, entry_count=count_cached_entries(p))
                for p in project.list_projects()
            ],
            "msg": msg,
            "crumbs": build_home_crumbs(PAGE_TITLE),
        },
    )


@router.get("/admin/export-cache/{project_name}")
def download_cache(project_name: str) -> Response:
    project_id = _known_project(project_name)
    return Response(
        content=export_stage_cache(project_id),
        media_type="application/zip",
        headers={
            "content-disposition": f'attachment; filename="{project_id}-cache.zip"'
        },
    )


@router.post("/admin/import-cache", response_class=HTMLResponse)
async def upload_cache(
    request: Request,
    file: UploadFile = File(...),
    destination: str = Form(...),
) -> Response:
    project_id = _known_project(destination)
    try:
        report = await run_in_threadpool(import_stage_cache, await file.read(), project_id)
    except CacheArchiveRejected as exc:
        return _redirect_to_cache_page(str(exc))
    return templates.TemplateResponse(
        request,
        "admin_cache_import.html",
        {
            "report": report,
            "destination": project_id,
            "crumbs": build_home_crumbs(PAGE_TITLE),
        },
    )


def _known_project(project_name: str) -> str:
    if project_name not in project.list_projects():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return project_name


def _redirect_to_cache_page(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin/cache?{urlencode({'msg': msg})}", status_code=303)
