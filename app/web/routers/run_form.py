"""Configuring a run: the form page, the JSON its pickers rebuild from when the version
changes, and the one multipart endpoint a file arrives through. Executing one is
app.web.routers.runs."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.services.errors import FileOverCeiling, StoreOverQuota
from app.services.uploads import max_upload_bytes, resolve_stored_path, save_upload
from app.web.file_sizes import describe_refusal
from app.services.versioning import list_project_versions
from app.web.breadcrumbs import build_runs_child_crumbs
from app.services.project import project_exists
from app.services.workspace import resolve_project_dir
from app.web.config import templates
from app.web.project_view import shell_state
from app.web.run_inputs import build_run_input_choices

router = APIRouter()


@router.get("/project/{project}/runs/new", response_class=HTMLResponse)
async def run_new(request: Request, project: str, version_id: str | None = None):
    if not project_exists(project):
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # Every stored version is runnable (resolve_version_id reads no publication
    # state), so the picker offers all of them newest-first. Registered ahead of
    # /runs/{run_id}, which would otherwise match "new" as a run id.
    versions = list_project_versions(project)
    # ?version_id= pre-picks one (the version page's "Run this version" sends it).
    # An id no version carries 404s rather than falling back to the latest, which
    # would launch a different workflow than the link named.
    if version_id is not None and not any(v.version_id == version_id for v in versions):
        raise HTTPException(status_code=404,
                            detail=f"No version '{version_id}' in project '{project}'")
    selected = version_id or (versions[0].version_id if versions else None)
    return templates.TemplateResponse(
        request,
        "section_run_new.html",
        {
            "state": shell_state(resolve_project_dir(project), "runs"),
            "section": "runs",
            "crumbs": build_runs_child_crumbs(project, label="New run"),
            "versions": versions,
            "selected_version_id": selected,
            "choices": build_run_input_choices(project, selected),
            # So Browse… can refuse an oversized pick before spending the upload on it.
            "max_upload_bytes": max_upload_bytes(),
        },
    )



@router.get("/project/{project}/run-inputs")
async def run_inputs(project: str, version_id: str | None = None):
    """The chosen version's file inputs AND the project's files, so one fetch rebuilds
    every picker."""
    if not project_exists(project):
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return JSONResponse(build_run_input_choices(project, version_id).model_dump())


@router.post("/project/{project}/files")
async def upload_file(project: str, file: UploadFile = File(...)):
    """One multipart endpoint for the run form's Browse… and for `curl -F file=@…`."""
    if not project_exists(project):
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    if not file.filename:
        return JSONResponse({"ok": False, "error": "no file provided"}, status_code=400)
    # Off the event loop: the copy streams a file of any size to disk and hashes it.
    try:
        record = await run_in_threadpool(save_upload, file.filename, file.file, project)
    except (FileOverCeiling, StoreOverQuota) as exc:
        # The wording names the limit and what to do; run_controls.js shows it verbatim.
        return JSONResponse({"ok": False, "error": describe_refusal(exc)}, status_code=400)
    # `sha256` is what a caller keeps — it names the file for a later run and is the
    # integrity check on the bytes it just sent. `path` is here for the run form,
    # whose field still submits a path.
    return JSONResponse({"ok": True, "sha256": record.sha256, "filename": record.filename,
                         "bytes": record.byte_count,
                         "path": str(resolve_stored_path(record))})
