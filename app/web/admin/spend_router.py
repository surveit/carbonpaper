"""The model-spend page: one figure for the whole workspace, off the records the
executor and the chat turn manager already write. Read-only, and its own router
rather than a route on `workspace_router`, which reaches the platform through
four named seams and none of them reads a run manifest.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.breadcrumbs import build_home_crumbs
from app.web.config import templates
from app.web.admin.spend import read_workspace_spend

router = APIRouter()

PAGE_TITLE = "Model spend"


@router.get("/admin/spend", response_class=HTMLResponse)
def spend_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_spend.html",
        {"spend": read_workspace_spend(), "crumbs": build_home_crumbs(PAGE_TITLE)},
    )
