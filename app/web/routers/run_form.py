"""Configuring a run: the form page, the JSON its pickers rebuild from when the version
changes, and the one multipart endpoint a file arrives through. Executing one is
app.web.routers.runs."""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from app.core.errors import FileOverCeiling, StoreOverQuota
from app.core.files import max_upload_bytes, save_upload
from app.web.file_sizes import describe_refusal
from app.services.versioning import list_versions
from app.web.breadcrumbs import build_runs_child_crumbs
from app.services.project import project_exists
from app.web.config import templates
from app.web.project_view import shell_state
from app.services.run_manifest_metadata import read_run_name
from app.web.loading import load_run_record
from app.web.run_inputs import build_run_input_choices, build_uploaded_file_choice

router = APIRouter()


@router.get("/project/{project_id}/runs/new", response_class=HTMLResponse)
def run_new(request: Request, project_id: str, version_id: str | None = None,
                  from_run: str | None = None):
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    # ?from_run= is Duplicate: it pre-fills only, so the reader still submits.
    copy_of = load_run_record(project_id, from_run) if from_run else None
    version_id = version_id or (copy_of.workflow_version if copy_of else None)
    # Every stored version is runnable (resolve_version_id reads no publication
    # state), so the picker offers all of them newest-first. Registered ahead of
    # /runs/{run_id}, which would otherwise match "new" as a run id.
    versions = list_versions(project_id)
    # ?version_id= pre-picks one (the version page's "Run this version" sends it).
    # An id no version carries 404s rather than falling back to the latest, which
    # would launch a different workflow than the link named.
    if version_id is not None and not any(v.version_id == version_id for v in versions):
        raise HTTPException(status_code=404,
                            detail=f"No version '{version_id}' in project '{project_id}'")
    selected = version_id or (versions[0].version_id if versions else None)
    return templates.TemplateResponse(
        request,
        "section_run_new.html",
        {
            "state": shell_state(project_id, "runs"),
            "section": "runs",
            "crumbs": build_runs_child_crumbs(project_id, label="New run"),
            "versions": versions,
            "selected_version_id": selected,
            "choices": build_run_input_choices(project_id, selected, copy_of),
            "copied_name": read_run_name(project_id, from_run) if from_run else "",
            # So Browse… can refuse an oversized pick before spending the upload on it.
            "max_upload_bytes": max_upload_bytes(),
        },
    )



@router.get("/project/{project_id}/run-inputs")
def run_inputs(project_id: str, version_id: str | None = None):
    """The chosen version's file inputs AND the project's files, so one fetch rebuilds
    every picker."""
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    return JSONResponse(build_run_input_choices(project_id, version_id).model_dump(mode="json"))


@router.post("/project/{project_id}/files")
async def upload_file(project_id: str, file: UploadFile = File(...)):
    """One multipart endpoint for the run form's Browse… and for `curl -F file=@…`."""
    if not project_exists(project_id):
        raise HTTPException(status_code=404, detail=f"No project '{project_id}'")
    if not file.filename:
        return JSONResponse({"ok": False, "error": "no file provided"}, status_code=400)
    # Off the event loop: the copy streams a file of any size to disk and hashes it.
    try:
        record = await run_in_threadpool(save_upload, file.filename, file.file, project_id)
    except (FileOverCeiling, StoreOverQuota) as exc:
        # The wording names the limit and what to do; run_controls.js shows it verbatim.
        return JSONResponse({"ok": False, "error": describe_refusal(exc)}, status_code=400)
    # `file_id` is what a caller keeps — it names this stored file for a later run. The
    # picker label is generated on the server, so an immediately inserted option matches
    # the next page render.
    return JSONResponse(build_uploaded_file_choice(record).model_dump(mode="json"))
