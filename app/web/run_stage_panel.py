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
from app.web.panel_links import AppPanelLinks


def resolve_panel_links(project: str, run_id: str) -> AppPanelLinks:
    # The app's one pick; the packet picks PacketPanelLinks in app/web/review_packet/pages.py.
    return AppPanelLinks(project, run_id)


def not_executed_panel(
    request: Request,
    project: str,
    run_id: str,
    manifest: dict[str, Any],
    stage_id: str,
    pinned: RunStageDef,
) -> HTMLResponse:
    """The panel for a stage this run's graph draws but its manifest has no record
    for: the definition the run pinned, plus why there is nothing from this run to
    show for it.

    Still a 404 when the stage is unknown to the run altogether — absent from the
    pinned version, or that version cannot be read. There is no definition to show
    then, and inventing a panel for it would be inventing the stage."""
    if pinned.stage is None:
        raise HTTPException(
            status_code=404, detail=pinned.error or f"No stage '{stage_id}' in run"
        )
    return templates.TemplateResponse(
        request,
        "_run_stage_not_executed.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": pinned.stage,
            "is_test_run": bool(manifest.get("parameters", {}).get("is_test_run")),
            "function_code": resolve_function_code(pinned.stage),
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )
