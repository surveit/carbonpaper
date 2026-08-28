"""Row provenance routes for a run: the show-your-work trace of one output row
(JSON and the read-only HTML story), and the lineage-trimmed stage panel that
story loads per stage. Split out of app.web.routers.runs — these three routes
share the trace machinery no other run route touches."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import (
    ContributorNotInFanIn,
    DocumentNotFound,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.runtime.errors import MissingLineage
from app.services.loader import resolve_function_code
from app.services import run as run_service
from app.runtime.trace import RowSampleChoice, Trace, trace_row, trace_to_dict
from app.models.workflow_stage import WorkflowStage
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.panel_links import AppPanelLinks, read_row_ref
from app.web import scope_view
from app.web.lineage_coordinate import build_lineage_coordinate
from app.web.row_paths import CitedFigure, NoPathsToShow, PathsPane, find_paths_behind_figure
from app.web.trace_inputs import build_input_catalog, read_run_inputs
from app.web.trace_view import build_trace_view, read_walked_rows
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import render_row_number, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.services.workspace import resolve_run_dir
from app.web.loading import load_manifest, load_run_record

router = APIRouter()


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/lineage_panel",
    response_class=HTMLResponse,
)
async def run_stage_lineage_panel(
    request: Request, project_id: str, run_id: str, stage_id: str, row: int
):
    manifest = load_manifest(project_id, run_id)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    # Transform detail is part of the lineage of THIS run, so it comes from the
    # version the run pinned. Unresolvable → no transform and a stated reason;
    # the page's own row view is unaffected, because that data is still true.
    pinned = run_service.load_pinned_stage_def(project_id, manifest, stage_id)
    stage_def = None if pinned.workflow_stage is None else pinned.workflow_stage.stage
    return templates.TemplateResponse(
        request,
        "_lineage_stage.html",
        {
            "project": project_id,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "workflow_stage": pinned.workflow_stage,
            "stage_def_error": pinned.error,
            "function_code": resolve_function_code(stage_def),
            "test_views": (lineage_views := shape_test_views(pinned.workflow_stage)),
            "certification": (
                build_certification(pinned.workflow_stage, lineage_views)
            ),
            "scoped_row": row,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


_VIA = Query(
    None,
    description="Which contributor to follow at a fan-in, as <stage_id>:<row_ordinal>.",
)


def _walk_row(project_id: str, run_id: str, stage_id: str, row: int,
              via: list[str] | None) -> Trace:
    """Crosses every fan-in it meets; `via` only says which contributor to take there."""
    run_dir = resolve_run_dir(project_id, run_id)
    try:
        return trace_row(run_dir, stage_id, row, _read_choices(via))
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (RowOutOfRange, ContributorNotInFanIn) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _read_choices(via: list[str] | None) -> list[RowSampleChoice]:
    try:
        return [RowSampleChoice(*read_row_ref(value)) for value in via or []]
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project_id: str, run_id: str, stage_id: str, row: int,
                              via: list[str] | None = _VIA):
    load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    return JSONResponse(
        trace_to_dict(_walk_row(project_id, run_id, stage_id, row, via)))


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
    response_class=HTMLResponse,
)
async def run_stage_row_trace_view(
    request: Request, project_id: str, run_id: str, stage_id: str, row: int,
    column: str | None = None, via: list[str] | None = _VIA,
):
    run_record = load_run_record(project_id, run_id)
    manifest = run_record.to_dict()
    stages_by_id = _read_run_stages(project_id, manifest)
    links = AppPanelLinks(project_id, run_id)
    view = _walk_row_into_view(project_id, run_id, stage_id, row, via, stages_by_id, links)
    ordered = [stages_by_id[n["stage_id"]].stage for n in view["nodes"]
               if n["stage_id"] in stages_by_id]
    mermaid = build_mermaid_graph(ordered, project_id) if len(ordered) == len(view["nodes"]) else ""
    coordinate = build_lineage_coordinate(
        run_record, view, stages_by_id.get(stage_id), column)
    # With no step walked there is no row to read a column off, so none is refused.
    if column is not None and coordinate.cells and coordinate.cell is None:
        raise HTTPException(
            status_code=400,
            detail=f"Stage '{stage_id}' in run '{run_id}' has no column '{column}'",
        )
    return templates.TemplateResponse(
        request,
        "lineage.html",
        {
            "title": f"{view['start_stage']} · row {render_row_number(view['start_row'])}",
            "view": view,
            "coordinate": coordinate,
            "inputs": read_run_inputs(build_input_catalog(project_id, manifest), links),
            "figure": CitedFigure(stage_id=stage_id, row_ordinal=row),
            "links": links,
            "project": project_id,
            "crumbs": build_run_child_crumbs(project_id, run_id, label="Row lineage"),
            "mermaid": mermaid,
        },
    )


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/paths/panel",
    response_class=HTMLResponse,
)
async def run_stage_row_paths_panel(
    request: Request, project_id: str, run_id: str, stage_id: str, row: int,
    via: list[str] | None = _VIA,
):
    manifest = load_run_record(project_id, run_id).to_dict()
    links = AppPanelLinks(project_id, run_id)
    view = _walk_row_into_view(project_id, run_id, stage_id, row, via,
                               _read_run_stages(project_id, manifest), links)
    return templates.TemplateResponse(
        request,
        "_row_paths_panel.html",
        {
            "pane": _read_paths_pane(project_id, run_id, stage_id, row, view),
            "figure": CitedFigure(stage_id=stage_id, row_ordinal=row),
            "links": links,
        },
    )


def _read_run_stages(project_id: str, manifest: dict[str, Any]) -> dict[str, WorkflowStage]:
    """Node detail and the graph describe THIS run, so both read the version it pinned."""
    try:
        return run_service.load_run_workflow(
            project_id, manifest).index_workflow_stages_by_id()
    except RunVersionUnresolvableError:
        # Never the working copy: the story still lists the ancestry, and no graph is drawn.
        return {}


def _walk_row_into_view(project_id: str, run_id: str, stage_id: str, row: int,
                        via: list[str] | None, stages_by_id: dict[str, WorkflowStage],
                        links: AppPanelLinks) -> dict[str, Any]:
    return build_trace_view(
        trace_to_dict(_walk_row(project_id, run_id, stage_id, row, via)), stages_by_id, links)


def _read_paths_pane(project_id: str, run_id: str, stage_id: str, row: int,
                     view: dict[str, Any]) -> PathsPane:
    """The page is the figure; its own walk says which of the paths below it took."""
    try:
        return find_paths_behind_figure(
            scope_view.read_run_branches(project_id, run_id),
            CitedFigure(stage_id=stage_id, row_ordinal=row), read_walked_rows(view),
        )
    # A run whose version no longer resolves has no branch options to read paths from.
    except (MissingLineage, StageNotInRun, RowOutOfRange, DocumentNotFound,
            FileNotFoundError, RunVersionUnresolvableError) as no_paths:
        return NoPathsToShow(reason=str(no_paths))


