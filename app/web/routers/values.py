"""Its own router because app.web.routers.runs is at the import fan-out ceiling."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.errors import (
    ColumnNotInFrame,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.web import values_view
from app.web.config import templates
from app.web.panel_links import AppPanelLinks

router = APIRouter()


@router.get(
    "/project/{project_id}/runs/{run_id}/values/panel", response_class=HTMLResponse
)
def values_panel(request: Request, project_id: str, run_id: str,
                       stage: str, row: int, column: str):
    """The column's walk, shell-less: the row lineage page holds it inside a tab."""
    try:
        payload = values_view.load_values_used(project_id, run_id, stage, column, row)
    except (ColumnNotInFrame, StageNotInRun, RunVersionUnresolvableError) as no_walk:
        # A pane that 404s shows the reader a browser error page inside a tab.
        return templates.TemplateResponse(
            request, "_values_panel.html",
            {"project": project_id, "run_id": run_id, "stage_id": stage,
             "column": column, "reason": str(no_walk)},
        )
    return templates.TemplateResponse(
        request, "_values_panel.html",
        {"project": project_id, "run_id": run_id, "stage_id": stage,
         "column": column, "values": payload,
         "links": AppPanelLinks(project_id, run_id),
         # What values-used.js steers by; the sheets themselves are already drawn.
         "nav": payload.model_dump(
             mode="json", include={"cited_stage", "edges", "sources"})},
    )
