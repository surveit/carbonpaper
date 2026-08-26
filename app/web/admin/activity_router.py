"""Its own router, not a route on `workspace_router`: no seam of that one reads a manifest."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.web.admin.activity import read_instance_activity
from app.web.breadcrumbs import build_home_crumbs
from app.web.config import templates

router = APIRouter()

PAGE_TITLE = "Instance activity"


@router.get("/admin/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "admin_activity.html",
        {"activity": read_instance_activity(), "crumbs": build_home_crumbs(PAGE_TITLE)},
    )
