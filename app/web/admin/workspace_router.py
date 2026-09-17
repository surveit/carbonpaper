"""Workspace admin page: load a packaged seed fixture, download a project as a
portable WorkflowFile document, or upload one back into the workspace.

Every path param is matched against a known list before use, so a request for an
unknown name 404s instead of reaching the seam with unsanitized input.
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from urllib.parse import urlencode
import zipfile

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.seeds.seed import discover_workflow_files
from app.services import project
from app.services.project import (
    CacheImportReport, ProjectArchiveRejected, ProjectImportReport, WorkflowFile,
    export_project, export_project_archive, import_bundle_file, import_project,
    import_project_archive, read_project_name,
)
from app.web.config import templates

router = APIRouter()


# ─── Path guards ───────────────────────────────────────────────────────────
# Every {bundle}/{project_name} below is checked against a list the seam
# itself just enumerated (discover_workflow_files() / list_projects()) —
# never a filesystem path built directly from the request.

def _bundle_path(bundle: str) -> Path:
    for candidate in discover_workflow_files():
        if candidate.stem == bundle:
            return candidate
    raise HTTPException(status_code=404, detail=f"No seed bundle '{bundle}'")


def _known_project(project_name: str) -> str:
    if project_name not in project.list_projects():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return project_name


def _redirect_to_admin(msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"/admin?{urlencode({'msg': msg})}", status_code=303)


# ─── Page ────────────────────────────────────────────────────────────────

@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request, msg: str | None = None):
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
def load_bundle(bundle: str):
    path = _bundle_path(bundle)
    try:
        report = import_bundle_file(path)
    except ProjectArchiveRejected as exc:
        raise HTTPException(status_code=400, detail=f"'{path.name}': {exc}") from exc
    # Loading a bundle twice makes a SECOND project rather than being refused: a label
    # is not unique, so there is nothing to clash with and nothing to overwrite. The
    # message names the id, which is the only half that tells the two of them apart.
    return _redirect_to_admin(
        f"Loaded '{read_project_name(report.project_id)}' ({report.project_id}) "
        f"from bundle '{bundle}'." + _say_what_the_cache_import_did(report.cache)
    )


@router.get("/admin/export/{project_name}")
def download_project(project_name: str) -> Response:
    project_id = _known_project(project_name)
    bundle = export_project(project_id)
    # The bundle's own name, which is the slug — a shown name may carry spaces and dashes.
    return Response(
        content=bundle.to_json(),
        media_type="application/json",
        headers={"content-disposition": f'attachment; filename="{bundle.name}.json"'},
    )


@router.get("/admin/export-with-cache/{project_name}")
def download_project_with_cache(project_name: str) -> Response:
    project_id = _known_project(project_name)
    # Named by id, as the cache export beside it is: the label rides inside the archive.
    return Response(
        content=export_project_archive(project_id),
        media_type="application/zip",
        headers={"content-disposition": f'attachment; filename="{project_id}.zip"'},
    )


@router.post("/admin/import")
async def upload_project(file: UploadFile = File(...)):
    raw = await file.read()
    return await run_in_threadpool(_do_upload_project, raw, file.filename)


def _do_upload_project(raw: bytes, filename: str | None) -> RedirectResponse:
    if zipfile.is_zipfile(BytesIO(raw)):
        report = _import_archive(raw, filename)
        return _redirect_to_admin(
            f"Imported '{read_project_name(report.project_id)}' ({report.project_id}) "
            "from an uploaded archive." + _say_what_the_cache_import_did(report.cache)
        )
    project_id = import_project(_parse_workflow_file(raw, filename))
    return _redirect_to_admin(
        f"Imported '{read_project_name(project_id)}' ({project_id}) from an uploaded file."
    )


def _import_archive(raw: bytes, filename: str | None) -> ProjectImportReport:
    try:
        return import_project_archive(raw)
    except ProjectArchiveRejected as exc:
        raise HTTPException(status_code=400, detail=f"'{filename}': {exc}") from exc
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a valid WorkflowFile document: {exc}",
        ) from exc


def _say_what_the_cache_import_did(cache: CacheImportReport | None) -> str:
    """Reachable is what says the cache will be READ, so it is in the sentence."""
    if cache is None:
        return ""
    stored = cache.written + cache.already_stored
    return (
        f" It brought {stored:,} cache rows, {cache.reachable:,} of them reachable "
        "from the stages that came with it."
    )


def _parse_workflow_file(raw: bytes, filename: str | None) -> WorkflowFile:
    try:
        return WorkflowFile.model_validate_json(raw)
    except ValidationError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"'{filename}' is not a valid WorkflowFile document: {exc}",
        ) from exc
