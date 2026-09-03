"""The operator's writes about a run: its name, and whether the index hides it."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import RedirectResponse

from app.models.records.run_manifest import RunManifest
from app.services.run_manifest_metadata import archive_run, name_run, unarchive_run
from app.web.project_view import validate_project_or_404

router = APIRouter()


@router.post("/project/{project_id}/runs/{run_id}/archive")
def archive_project_run(project_id: str, run_id: str):
    _refuse_unrecorded_run(validate_project_or_404(project_id), run_id)
    archive_run(project_id, run_id)
    return RedirectResponse(url=f"/project/{project_id}/runs", status_code=303)


@router.post("/project/{project_id}/runs/{run_id}/unarchive")
def unarchive_project_run(project_id: str, run_id: str):
    _refuse_unrecorded_run(validate_project_or_404(project_id), run_id)
    unarchive_run(project_id, run_id)
    return RedirectResponse(
        url=f"/project/{project_id}/runs?view=archived", status_code=303
    )


@router.post("/project/{project_id}/runs/{run_id}/name")
def name_project_run(project_id: str, run_id: str, name: str = Form(default="")):
    _refuse_unrecorded_run(validate_project_or_404(project_id), run_id)
    name_run(project_id, run_id, name)
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}", status_code=303
    )


def _refuse_unrecorded_run(project_id: str, run_id: str) -> None:
    # Asked of the store, not of a parsed manifest: an unreadable run is archivable too.
    if not RunManifest.exists(RunManifest.compose_id(project_id, run_id)):
        raise HTTPException(
            status_code=404, detail=f"no run '{run_id}' in project '{project_id}'"
        )
