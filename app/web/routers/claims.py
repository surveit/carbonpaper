"""The claims a project has made: the list, the page for one, and the writes behind them."""
from __future__ import annotations

import logging
from typing import Callable, TypeVar

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.errors import ClaimAttackRefused
from app.models.records.claims import Claim
from app.services import claim_review
from app.services import claims as claims_service
from app.services import project as project_service
from app.services import run as run_service
from app.services.errors import ClaimRefused, ClaimReviewRefused
from app.web.breadcrumbs import Crumb, build_run_child_crumbs, build_section_crumbs
from app.web.claim_review_view import build_claim_review_page
from app.web.claims_list_view import build_claims_list_page
from app.web.claims_view import build_publish_view
from app.web.config import templates
from app.web.project_view import shell_state, shell_state_off_nav, validate_project_or_404
from app.web.run_index import RunIndexRow, find_run_row

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


@router.get("/project/{project_id}/claims", response_class=HTMLResponse)
async def claims_page(request: Request, project_id: str):
    validate_project_or_404(project_id)
    return templates.TemplateResponse(
        request,
        "claims.html",
        {
            "state": shell_state(project_id, "claims"),
            "section": "claims",
            "page": _refusing_404(lambda: build_claims_list_page(project_id)),
        },
    )


@router.get("/project/{project_id}/claims/{claim_id}", response_class=HTMLResponse)
async def read_claim_review_page(request: Request, project_id: str, claim_id: str):
    validate_project_or_404(project_id)
    page = _refusing_404(lambda: build_claim_review_page(project_id, claim_id))
    return templates.TemplateResponse(
        request,
        "claim_review.html",
        {
            "state": shell_state_off_nav(project_id, _build_claim_crumbs(project_id, page.run_id)),
            "section": "runs",
            "page": page,
        },
    )


@router.post("/project/{project_id}/claims/{claim_id}/approve")
async def approve_claim(project_id: str, claim_id: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.approve_claim(
        project_id, claim_id, _read_whether_the_run_read_everything(project_id, claim_id)))
    return _back_to_the_claim(project_id, claim_id)


@router.post("/project/{project_id}/claims/{claim_id}/decline")
async def decline_claim(project_id: str, claim_id: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.decline_claim(project_id, claim_id))
    return _back_to_the_claim(project_id, claim_id)


@router.post("/project/{project_id}/claims/{claim_id}/rewrite")
async def rewrite_claim(request: Request, project_id: str, claim_id: str):
    # async: start_claim_attack calls asyncio.create_task, needing a running loop.
    validate_project_or_404(project_id)
    form = await request.form()
    written = _refusing_400(
        lambda: _write_the_rewrite(project_id, claim_id, str(form.get("text", ""))))
    _attack_what_the_journalist_wrote(project_id, written.id)
    return _back_to_the_claim(project_id, written.id)


@router.post("/project/{project_id}/claims/{claim_id}/attack")
async def attack_claim(project_id: str, claim_id: str):
    # async: start_claim_attack calls asyncio.create_task, needing a running loop.
    validate_project_or_404(project_id)
    session_id = _refusing_400(lambda: claim_review.start_claim_attack(
        project_id, claim_id, model=_read_model(project_id)))
    return JSONResponse({"ok": True, "session": session_id})


@router.post("/project/{project_id}/runs/{run_id}/skip/{slug}")
async def skip_output(project_id: str, run_id: str, slug: str):
    validate_project_or_404(project_id)
    _refusing_400(lambda: claims_service.decline_output(project_id, run_id, slug))
    return _back_to_the_page(project_id, run_id)


def _attack_what_the_journalist_wrote(project_id: str, claim_id: str) -> None:
    """A claim whose attack could not start still stands; its page will say it was not attacked."""
    try:
        claim_review.start_claim_attack(project_id, claim_id, model=_read_model(project_id))
    except (ClaimAttackRefused, OSError) as exc:
        _LOG.warning("claim %s stands submitted but was not attacked: %s", claim_id, exc)


def _write_the_rewrite(project_id: str, claim_id: str, text: str) -> Claim:
    """The rewrite is a claim of its own; submit_claim supersedes the one it restates."""
    standing = claims_service.load_claim(project_id, claim_id)
    output = claims_service.find_output_of_claim(standing)
    return claims_service.submit_claim(
        project_id, standing.citation.run_id, output.slug, standing.context, text)


def _read_whether_the_run_read_everything(project_id: str, claim_id: str) -> bool:
    run_id = claims_service.load_claim(project_id, claim_id).citation.run_id
    return claims_service.read_whether_the_run_read_everything(
        run_service.read_run_manifest(project_id, run_id))


def _read_model(project_id: str) -> str:
    return project_service.project_meta(project_id).model or "sonnet"


def _read_run(project_id: str, run_id: str) -> RunIndexRow:
    row = find_run_row(project_id, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"no run '{run_id}' in this project")
    return row


def _refusing_400(write: Callable[[], _Written]) -> _Written:
    try:
        return write()
    except (ClaimRefused, ClaimReviewRefused) as exc:
        raise HTTPException(status_code=400, detail="; ".join(exc.refusals)) from exc


def _refusing_404(read: Callable[[], _Written]) -> _Written:
    try:
        return read()
    except ClaimRefused as exc:
        raise HTTPException(status_code=404, detail="; ".join(exc.refusals)) from exc


def _back_to_the_page(project_id: str, run_id: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}/publish", status_code=303
    )


def _back_to_the_claim(project_id: str, claim_id: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/project/{project_id}/claims/{claim_id}", status_code=303
    )


def _crumbs(project_id: str) -> list[Crumb]:
    return build_section_crumbs(
        project_id, label="Publish", parent=("Runs", f"/project/{project_id}/runs")
    )


def _build_claim_crumbs(project_id: str, run_id: str) -> list[Crumb]:
    return build_run_child_crumbs(project_id, run_id, label="Claim")
