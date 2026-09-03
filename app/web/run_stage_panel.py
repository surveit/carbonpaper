"""The run stage panel's not-executed case: a stage the run's graph draws (the graph
comes from the whole pinned version) but the run never executed, so its manifest holds
no record for it. Its own module because app.web.routers.runs and .run_stage both
resolve their panel links through it."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse

from app.services.loader import resolve_function_code
from app.services.run import RunStageDef
from app.web.config import templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH
from app.web.loading import find_queue_snapshot_path
from app.web.panel_links import AppPanelLinks


def resolve_panel_links(project_id: str, run_id: str) -> AppPanelLinks:
    return AppPanelLinks(project_id, run_id)


def find_queue_link(
    links: AppPanelLinks, project_id: str, run_id: str, stage_id: str
) -> str | None:
    """None where this run left no queue here; a page reading "no items" is a dead link."""
    if find_queue_snapshot_path(project_id, run_id, stage_id) is None:
        return None
    return links.review_queue(stage_id)


def not_executed_panel(
    request: Request,
    project_id: str,
    run_id: str,
    manifest: dict[str, Any],
    stage_id: str,
    pinned: RunStageDef,
) -> HTMLResponse:
    if pinned.workflow_stage is None:
        raise HTTPException(
            status_code=404, detail=pinned.error or f"No stage '{stage_id}' in run"
        )
    stage = pinned.workflow_stage.stage
    return templates.TemplateResponse(
        request,
        "_run_stage_not_executed.html",
        {
            "project": project_id,
            "run_id": run_id,
            "stage": stage,
            "workflow_stage": pinned.workflow_stage,
            "is_test_run": bool(manifest.get("parameters", {}).get("is_test_run")),
            "function_code": resolve_function_code(stage),
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )
