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
from app.web.loading import load_manifest
from app.web.run_stage_view import build_run_stage_panel

router = APIRouter()

# A pane that 404s shows a browser error page inside a tab; these answer 200.
NO_WALK = (ColumnNotInFrame, StageNotInRun, RunVersionUnresolvableError)


@router.get(
    "/project/{project_id}/runs/{run_id}/values/panel", response_class=HTMLResponse
)
async def values_panel(request: Request, project_id: str, run_id: str,
                       stage: str, row: int, column: str):
    """The column's walk, shell-less: the row lineage page holds it inside a tab."""
    try:
        payload = values_view.load_values_used(project_id, run_id, stage, column, row)
    except NO_WALK as no_walk:
        return templates.TemplateResponse(
            request, "_values_panel.html",
            {"project": project_id, "run_id": run_id, "stage_id": stage,
             "column": column, "reason": str(no_walk)},
        )
    return templates.TemplateResponse(
        request, "_values_panel.html",
        {"project": project_id, "run_id": run_id, "stage_id": stage,
         "column": column, "values": payload,
         # What values-used.js steers by; each stage's panel is fetched.
         "nav": payload.model_dump(
             mode="json", include={"cited_stage", "edges", "column", "sources"})},
    )


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/traced",
    response_class=HTMLResponse,
)
async def traced_stage_panel(request: Request, project_id: str, run_id: str,
                             stage_id: str, stage: str, row: int, column: str):
    """The run page's own stage panel, cut to the rows behind one figure."""
    manifest = load_manifest(project_id, run_id)
    record = next((entry for entry in manifest.get("stage_records", [])
                   if entry.get("stage_id") == stage_id), None)
    try:
        scope = values_view.build_trace_scope(project_id, run_id, stage, column, row)
    except NO_WALK as no_scope:
        return _say_why_no_panel(request, stage_id, str(no_scope))
    if record is None:
        return _say_why_no_panel(request, stage_id,
                                 "this run has no record of the stage")
    panel = build_run_stage_panel(
        project_id, run_id, stage_id, manifest, record, scope=scope)
    return templates.TemplateResponse(
        request, "_run_stage_panel.html", panel.as_context())


def _say_why_no_panel(request: Request, stage_id: str, reason: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "_no_stage_panel.html", {"stage_id": stage_id, "reason": reason})
