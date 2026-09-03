"""Publishing a run: the page that offers its claims, and the four writes behind it."""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services import claims as claims_service
from app.services.errors import ClaimRefused
from app.web.breadcrumbs import Crumb, build_section_crumbs
from app.web.claims_view import build_publish_view, read_whether_the_run_read_everything
from app.web.config import templates
from app.web.project_view import shell_state_off_nav, validate_project_or_404
from app.web.run_index import RunIndexRow, build_run_index_rows

router = APIRouter()

_CONTEXT_PREFIX = "context."


@router.get("/project/{project_id}/runs/{run_id}/publish", response_class=HTMLResponse)
async def publish_run_page(request: Request, project_id: str, run_id: str):
    validate_project_or_404(project_id)
    return templates.TemplateResponse(
        request,
        "publish_run.html",
        {
            "state": shell_state_off_nav(project_id, _crumbs(project_id)),
            "section": "runs",
            "publish": build_publish_view(project_id, _read_run(project_id, run_id)),
        },
    )


@router.post("/project/{project_id}/runs/{run_id}/submit/{slug}")
async def submit_claim(request: Request, project_id: str, run_id: str, slug: str):
    validate_project_or_404(project_id)
    form = await request.form()
    context = {
        key[len(_CONTEXT_PREFIX):]: str(value)
        for key, value in form.multi_items()
        if key.startswith(_CONTEXT_PREFIX)
    }
    _refusing_400(lambda: claims_service.submit_claim(
        project_id, run_id, slug, context, str(form.get("text", ""))
    ))
    return _back_to_the_page(project_id, run_id)


@router.post("/project/{project_id}/runs/{run_id}/approve/{claim_id}")
async def approve_claim(project_id: str, run_id: str, claim_id: str):
    validate_project_or_404(project_id)
    run = _read_run(project_id, run_id)
    _refusing_400(lambda: claims_service.approve_claim(
        project_id, claim_id, read_whether_the_run_read_everything(run)
    ))
    return _back_to_the_page(project_id, run_id)


@router.post("/project/{project_id}/runs/{run_id}/reject/{claim_id}")
async def reject_claim(project_id: str, run_id: str, claim_id: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.decline_claim(project_id, claim_id))
    return _back_to_the_page(project_id, run_id)


@router.post("/project/{project_id}/runs/{run_id}/skip/{slug}")
async def skip_output(project_id: str, run_id: str, slug: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.decline_output(project_id, run_id, slug))
    return _back_to_the_page(project_id, run_id)


def _read_run(project_id: str, run_id: str) -> RunIndexRow:
    for row in build_run_index_rows(project_id):
        if row.run_id == run_id:
            return row
    raise HTTPException(status_code=404, detail=f"no run '{run_id}' in this project")


def _refusing_400(write: Callable[[], object]) -> None:
    try:
        write()
    except ClaimRefused as exc:
        raise HTTPException(status_code=400, detail="; ".join(exc.refusals)) from exc


def _back_to_the_page(project_id: str, run_id: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}/publish", status_code=303
    )


def _crumbs(project_id: str) -> list[Crumb]:
    return build_section_crumbs(
        project_id, label="Publish", parent=("Runs", f"/project/{project_id}/runs")
    )
