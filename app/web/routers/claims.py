"""Publishing a run: the page that offers its claims, and the four writes behind it."""
from __future__ import annotations

import logging
from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.services import claim_review
from app.services import claims as claims_service
from app.services import project as project_service
from app.services.errors import ClaimRefused, ClaimReviewRefused
from app.web.breadcrumbs import Crumb, build_section_crumbs
from app.web.claims_view import build_publish_view
from app.web.config import templates
from app.web.project_view import shell_state_off_nav, validate_project_or_404
from app.web.run_index import RunIndexRow, build_run_index_rows

router = APIRouter()

_LOG = logging.getLogger(__name__)

_CONTEXT_PREFIX = "context."

_Written = TypeVar("_Written")


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
    # async: start_claim_attack calls asyncio.create_task, needing a running loop.
    validate_project_or_404(project_id)
    form = await request.form()
    context = {
        key[len(_CONTEXT_PREFIX):]: str(value)
        for key, value in form.multi_items()
        if key.startswith(_CONTEXT_PREFIX)
    }
    claim = _refusing_400(lambda: claims_service.submit_claim(
        project_id, run_id, slug, context, str(form.get("text", ""))
    ))
    _attack_what_the_journalist_wrote(project_id, claim.id)
    return _back_to_the_page(project_id, run_id)


@router.post("/project/{project_id}/claims/{claim_id}/attack")
async def attack_claim(project_id: str, claim_id: str):
    # async: start_claim_attack calls asyncio.create_task, needing a running loop.
    validate_project_or_404(project_id)
    session_id = _refusing_400(lambda: claim_review.start_claim_attack(
        project_id, claim_id, model=_model_of(project_id)))
    return JSONResponse({"ok": True, "session": session_id})


@router.post("/project/{project_id}/runs/{run_id}/skip/{slug}")
async def skip_output(project_id: str, run_id: str, slug: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.decline_output(project_id, run_id, slug))
    return _back_to_the_page(project_id, run_id)


def _attack_what_the_journalist_wrote(project_id: str, claim_id: str) -> None:
    """A claim there is nothing to attack still stands; its page says it was not attacked."""
    try:
        claim_review.start_claim_attack(project_id, claim_id, model=_model_of(project_id))
    except ClaimReviewRefused as exc:
        _LOG.warning("claim %s stands submitted but was not attacked: %s", claim_id, exc)


def _model_of(project_id: str) -> str:
    return project_service.project_meta(project_id).model or "sonnet"


def _read_run(project_id: str, run_id: str) -> RunIndexRow:
    for row in build_run_index_rows(project_id):
        if row.run_id == run_id:
            return row
    raise HTTPException(status_code=404, detail=f"no run '{run_id}' in this project")


def _refusing_400(write: Callable[[], _Written]) -> _Written:
    try:
        return write()
    except (ClaimRefused, ClaimReviewRefused) as exc:
        raise HTTPException(status_code=400, detail="; ".join(exc.refusals)) from exc


def _back_to_the_page(project_id: str, run_id: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}/publish", status_code=303
    )


def _crumbs(project_id: str) -> list[Crumb]:
    return build_section_crumbs(
        project_id, label="Publish", parent=("Runs", f"/project/{project_id}/runs")
    )
