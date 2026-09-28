"""Comparing two runs of one workflow version: app.services.run_diff, drawn by run_compare.html."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.core.errors import RunNotFoundError, RunVersionUnresolvableError
from app.services.errors import RunComparisonRefused
from app.services.run_diff import compare_runs
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.project_view import shell_state_off_nav, validate_project_or_404
from app.web.run_diff_view import build_run_compare_page

router = APIRouter()


@router.get(
    "/project/{project_id}/runs/{run_id}/compare/{other_run_id}", response_class=HTMLResponse
)
def compare_runs_page(request: Request, project_id: str, run_id: str, other_run_id: str):
    validate_project_or_404(project_id)
    try:
        comparison = compare_runs(project_id, run_id, other_run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RunComparisonRefused, RunVersionUnresolvableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    crumbs = build_run_child_crumbs(project_id, run_id, label=f"Compared with {other_run_id}")
    return templates.TemplateResponse(
        request,
        "run_compare.html",
        {
            "state": shell_state_off_nav(project_id, crumbs),
            "section": "runs",
            "page": build_run_compare_page(project_id, comparison),
        },
    )
