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
from app.web.trace_view import build_trace_view
from app.web.config import templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import load_manifest, load_output_row, runs_dir

router = APIRouter()


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/lineage_panel",
    response_class=HTMLResponse,
)
async def run_stage_lineage_panel(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """Minimal stage view for the lineage page, its output trimmed to `row`."""
    # Reuses `_stage_executable.html` and `schema_table` — not the whole
    # run-detail panel.
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")
    # Transform detail is part of the lineage of THIS run, so it comes from the
    # version the run pinned. Unresolvable → no transform and a stated reason;
    # the row's output table still renders, because that data is still true.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    return templates.TemplateResponse(
        request,
        "_lineage_stage.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": pinned.stage,
            "stage_def_error": pinned.error,
            "function_code": resolve_function_code(pinned.stage),
            "test_views": (lineage_views := shape_test_views(pinned.stage)),
            "certification": (
                build_certification(pinned.stage, lineage_views) if pinned.stage else None
            ),
            "preview": load_output_row(run_dir, stage_record.get("output_path"), row),
            "scoped_row": row,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace")
async def run_stage_row_trace(project: str, run_id: str, stage_id: str, row: int):
    """One row's ancestry through row-preserving stages, as JSON."""
    # 404 if the run/stage is absent, 400 if the row ordinal is out of range.
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    try:
        trace = trace_row(run_dir, stage_id, row)
    except StageNotInRun as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RowOutOfRange as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(trace_to_dict(trace))


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/row/{row}/trace/view",
    response_class=HTMLResponse,
)
async def run_stage_row_trace_view(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """The row's show-your-work as a read-only HTML page."""
    # A numbered story and a graph toggle on top; clicking a stage loads the
    # row-trimmed panel below.
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
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
        stages = run_service.load_run_stages(project, manifest)
    except RunVersionUnresolvableError:
        stages = []
    stages_by_id = {s.id: s for s in stages}

    view = build_trace_view(trace_to_dict(trace), stages_by_id)
    ordered = [stages_by_id[n["stage_id"]] for n in view["nodes"]
               if n["stage_id"] in stages_by_id]
    mermaid = build_mermaid_graph(ordered, project) if len(ordered) == len(view["nodes"]) else ""
    return templates.TemplateResponse(
        request,
        "lineage.html",
        {
            "title": f"{view['start_stage']} · row {view['start_row']}",
            "view": view,
            "project": project,
            "mermaid": mermaid,
        },
    )
