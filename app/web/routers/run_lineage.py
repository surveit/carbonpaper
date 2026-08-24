"""Row provenance routes for a run: the show-your-work trace of one output row
(JSON and the read-only HTML story), and the lineage-trimmed stage panel that
story loads per stage. Split out of app.web.routers.runs — these three routes
share the trace machinery no other run route touches."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import (
    DocumentNotFound,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.runtime.errors import MissingLineage
from app.services.loader import resolve_function_code
from app.services import run as run_service
from app.runtime.trace import trace_row, trace_to_dict
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.panel_links import AppPanelLinks
from app.web.row_paths import CitedFigure, NoPathsToShow, PathsPane, find_paths_behind
from app.web import scope_view
from app.web.trace_view import build_trace_view, find_cited_cell
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.services.workspace import resolve_run_dir
from app.web.loading import load_manifest

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


@router.get("/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project_id: str, run_id: str, stage_id: str, row: int):
    run_dir = resolve_run_dir(project_id, run_id)
    load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    try:
        trace = trace_row(run_dir, stage_id, row)
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RowOutOfRange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(trace_to_dict(trace))


@router.get(
    "/project/{project_id}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
    response_class=HTMLResponse,
)
async def run_stage_row_trace_view(
    request: Request, project_id: str, run_id: str, stage_id: str, row: int,
    column: str | None = None, figure: str | None = None,
):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)
    try:
        trace = trace_row(run_dir, stage_id, row)
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RowOutOfRange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # Node detail and the graph both describe THIS run, so both read the version
    # it pinned. With no resolvable version neither falls back to the working
    # copy: the story still lists the ancestry, transforms show as "unknown",
    # and no graph is drawn.
    try:
        stages_by_id = run_service.load_run_workflow(
            project_id, manifest).index_workflow_stages_by_id()
    except RunVersionUnresolvableError:
        stages_by_id = {}

    view = build_trace_view(trace_to_dict(trace), stages_by_id, AppPanelLinks(project_id, run_id))
    ordered = [stages_by_id[n["stage_id"]].stage for n in view["nodes"]
               if n["stage_id"] in stages_by_id]
    mermaid = build_mermaid_graph(ordered, project_id) if len(ordered) == len(view["nodes"]) else ""
    # With no step walked there is no row to read a cited cell from.
    cell = None
    if column is not None and view["nodes"]:
        cell = find_cited_cell(view, stages_by_id.get(stage_id), column)
        if cell is None:
            raise HTTPException(
                status_code=400,
                detail=f"Stage '{stage_id}' in run '{run_id}' has no column '{column}'",
            )
    return templates.TemplateResponse(
        request,
        "lineage.html",
        {
            "title": f"{view['start_stage']} · row {view['start_row']}",
            "view": view,
            "cell": cell,
            "pane": (pane := _read_paths_pane(project_id, run_id, stage_id, row, figure)),
            "feeds": _say_which_figure_it_feeds(
                pane, AppPanelLinks(project_id, run_id), stage_id, row),
            "project": project_id,
            "crumbs": build_run_child_crumbs(project_id, run_id, label="Row lineage"),
            "mermaid": mermaid,
        },
    )


def _read_paths_pane(project_id: str, run_id: str, stage_id: str, row: int,
                     figure: str | None) -> PathsPane:
    """`figure` holds the pane on the row the reader came from as they change path."""
    cited = _read_figure(figure) or CitedFigure(stage_id=stage_id, row_ordinal=row)
    try:
        return find_paths_behind(
            scope_view.read_run_branches(project_id, run_id),
            AppPanelLinks(project_id, run_id), cited, stage_id, row,
        )
    # A run whose version no longer resolves has no branch options to read paths from.
    except (MissingLineage, StageNotInRun, RowOutOfRange, DocumentNotFound,
            FileNotFoundError, RunVersionUnresolvableError) as no_paths:
        return NoPathsToShow(reason=str(no_paths))


def _read_figure(figure: str | None) -> CitedFigure | None:
    if figure is None:
        return None
    stage_id, _, ordinal = figure.rpartition(":")
    if not stage_id or not ordinal.isdigit():
        raise HTTPException(
            status_code=400,
            detail=f"malformed figure {figure!r} — expected stage_id:row",
        )
    return CitedFigure(stage_id=stage_id, row_ordinal=int(ordinal))


@dataclass(frozen=True)
class FeedsAFigure:
    """Shown only where the reader arrived from a figure this row is one of many behind."""

    stage_id: str
    row_ordinal: int
    rows: int
    href: str


def _say_which_figure_it_feeds(
    pane: PathsPane, links: AppPanelLinks, stage_id: str, row: int
) -> FeedsAFigure | None:
    if isinstance(pane, NoPathsToShow):
        return None
    figure = pane.figure
    if (figure.stage_id, figure.row_ordinal) == (stage_id, row):
        return None
    return FeedsAFigure(
        stage_id=figure.stage_id, row_ordinal=figure.row_ordinal, rows=pane.rows,
        href=links.row_trace(figure.stage_id, figure.row_ordinal),
    )
