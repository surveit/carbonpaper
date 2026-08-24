"""Row provenance routes for a run: the show-your-work trace of one output row
(JSON and the read-only HTML story), and the lineage-trimmed stage panel that
story loads per stage. Split out of app.web.routers.runs — these three routes
share the trace machinery no other run route touches."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import RowOutOfRange, RunVersionUnresolvableError, StageNotInRun
from app.services.loader import resolve_function_code
from app.services import run as run_service
from app.runtime.trace import trace_row, trace_to_dict
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.panel_links import AppPanelLinks
from app.web.lineage_coordinate import build_lineage_coordinate
from app.web.trace_view import build_trace_view
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
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
    column: str | None = None,
):
    run_dir = resolve_run_dir(project_id, run_id)
    run_record = load_run_record(project_id, run_id)
    manifest = run_record.to_dict()
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
            "title": f"{view['start_stage']} · row {view['start_row']}",
            "view": view,
            "coordinate": coordinate,
            "project": project_id,
            "crumbs": build_run_child_crumbs(project_id, run_id, label="Row lineage"),
            "mermaid": mermaid,
        },
    )
