"""Run lifecycle: trigger a run, list runs, poll live status, render a run's detail,
serve its artifacts, resume and cancel. Configuring one — the form, its pickers and the
file endpoint they post to — is app.web.routers.run_form; the per-stage panel, its row
views and the scratch re-run are app.web.routers.run_stage."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.datastructures import FormData
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)

from app.core.errors import (
    FileNotStoredError,
    MissingInputBindingError,
    NoVersionToRunError,
    RunVersionUnresolvableError,
)
from app.core.run_status import RunStatus, StageStatus
from app.models import WorkflowStage
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.services.errors import WorkflowLoadError
from app.services import run as run_service
from app.services.run_guide import build_run_guide_view
from app.services.uploads import resolve_files_binding
from app.runtime.cancellation import request_cancel
from app.web.breadcrumbs import build_run_crumbs
from app.web.config import EVENT_TAIL, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.services.workspace import resolve_run_dir
from app.web.loading import (
    load_manifest,
)
from app.web.project_view import shell_state, validate_project_or_404
from app.web.run_events import (
    EVENT_PAGE_MAX,
    page_events_before,
    stream_events,
    tail_start_seq,
)
from app.web.run_header import build_live_view, build_run_header
from app.web.run_index import (
    build_run_status_choices,
    RUN_VIEW_PRODUCTION,
    RUN_VIEWS,
    build_run_index_rows,
    build_run_view_choices,
)
from app.web.run_issues import build_run_issues
from app.web.run_stage_panel import resolve_panel_links

router = APIRouter()

@router.post("/project/{project_id}/run")
async def trigger_run(request: Request, project_id: str):
    validate_project_or_404(project_id)
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    try:
        form = await request.form()
        version_id = str(form.get("version_id") or "").strip() or None
        bindings = _collect_bindings(form, project_id)
        limits = _collect_limits(form)
        run_id = run_service.start_run(project_id, version_id=version_id,
                                       bindings=bindings, limits=limits,
                                       bust_cache=_read_bust_cache(form),
                                       name=str(form.get("name") or ""))
    except (FileNotStoredError, NoVersionToRunError, MissingInputBindingError,
            ValueError) as exc:
        # ValueError here is limit/offset validation failures raised by _collect_limits,
        # apply_run_bindings or prepare_run — not a catch-all for other bugs.
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}",
        status_code=303,
    )


def _collect_bindings(form: FormData, project_id: str) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    """`binding__<stage>` repeats once per file; none means run what the workflow authored."""
    bindings: dict[StageId, TypeUnsafeUserStageConfigOverride] = {}
    for key in {key for key in form.keys() if key.startswith("binding__")}:
        file_ids = [str(value).strip() for value in form.getlist(key)]
        chosen = [file_id for file_id in file_ids if file_id]
        if chosen:
            bindings[key[len("binding__"):]] = resolve_files_binding(project_id, chosen)
    return bindings


def _collect_limits(form: FormData) -> dict[str, int]:
    limits: dict[str, int] = {}
    for key, value in form.items():
        if not key.startswith("limit__"):
            continue
        stage_id = key[len("limit__"):]
        text = str(value).strip()
        if not text:
            continue
        if not text.isdecimal():
            raise ValueError(
                f"row limit for stage '{stage_id}' must be a non-negative "
                f"whole number, got {value!r}"
            )
        limits[stage_id] = int(text)
    return limits


def _read_bust_cache(form: FormData) -> bool:
    return "bust_cache" in form


@router.get("/project/{project_id}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, project_id: str,
                     view: str = RUN_VIEW_PRODUCTION, inputs: str = "", file: str = "",
                     status: str = ""):
    validate_project_or_404(project_id)
    if view not in RUN_VIEWS:
        raise HTTPException(status_code=400, detail=f"unknown runs view '{view}'")
    # A stored version that no longer validates raises WorkflowLoadError from
    # any listing/load (shell_state's version count included) and fails this
    # page loudly — the remedy is a store migration, not a tolerant render.
    rows = build_run_index_rows(project_id, view=view, input_key=inputs, file_sha256=file)
    return templates.TemplateResponse(
        request,
        "section_runs.html",
        {
            "state": shell_state(project_id, "runs"),
            "section": "runs",
            "runs": [row for row in rows if not status or row.status == status],
            "view": view,
            "view_choices": build_run_view_choices(project_id),
            # Built from the unfiltered rows, so picking one status still lists the others.
            "status_choices": build_run_status_choices(rows),
            "status_filter": status,
            "input_filter": inputs,
            "file_filter": file,
        },
    )


@router.get("/project/{project_id}/runs/{run_id}/status")
async def run_status(project_id: str, run_id: str):
    manifest = load_manifest(project_id, run_id)
    mstages = manifest.get("stage_records", [])
    status_by_id = {s["stage_id"]: s.get("status", "") for s in mstages}
    graph = build_run_graph(project_id, manifest, status_by_id)

    def _count(st: StageStatus) -> int:
        return sum(1 for s in mstages if s.get("status") == st)

    return JSONResponse({
        "status": manifest.get("status"),
        "terminal": manifest.get("status") != RunStatus.RUNNING,
        "halted_at": manifest.get("halted_at"),
        "finished_at": manifest.get("finished_at"),
        "counts": {"ok": _count(StageStatus.OK), "warn": _count(StageStatus.VALIDATION_WARNINGS),
                   "err": _count(StageStatus.ERROR), "total": len(mstages),
                   "done": _count(StageStatus.OK) + _count(StageStatus.VALIDATION_WARNINGS),
                   "running": _count(StageStatus.RUNNING), "pending": _count(StageStatus.PENDING),
                   "awaiting": _count(StageStatus.AWAITING_REVIEW),
                   "cancelled": _count(StageStatus.CANCELLED)},
        "stages": [
            {
                "stage_id": s["stage_id"],
                "status": s.get("status"),
                "progress": s.get("progress"),
            }
            for s in mstages
        ],
        # The header parts that move while a run is in flight; the run page
        # updates them in place rather than fetching a second endpoint.
        "header": build_live_view(project_id, run_id, manifest).model_dump(),
        "mermaid": graph.mermaid,
        "graph_error": graph.error,
    })


@dataclass(frozen=True)
class RunGraph:
    """Either stages plus mermaid, or error with stages None — never both."""

    stages: list[WorkflowStage] | None
    mermaid: str
    error: str | None


def build_run_graph(
    project_id: str, manifest: dict[str, Any], status_by_id: dict[str, str]
) -> RunGraph:
    try:
        workflow = run_service.load_run_workflow(project_id, manifest)
    except RunVersionUnresolvableError as exc:
        return RunGraph(stages=None, mermaid="", error=str(exc))
    workflow_stages = workflow.list_workflow_stages()
    return RunGraph(
        stages=workflow_stages,
        mermaid=build_mermaid_graph(
            [resolved.stage for resolved in workflow_stages],
            project_id, status_by_id=status_by_id),
        error=None,
    )


@router.get("/project/{project_id}/runs/{run_id}/events")
async def stream_run_events(
    project_id: str,
    run_id: str,
    request: Request,
    from_seq: int | None = None,
    tail: int = EVENT_TAIL,
    stage: str | None = None,
):
    load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    start = (
        tail_start_seq(project_id, run_id, tail, stage)
        if from_seq is None
        else max(from_seq, 0)
    )
    return StreamingResponse(
        stream_events(project_id, run_id, request, start, stage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/project/{project_id}/runs/{run_id}/events/page")
async def run_events_page(
    project_id: str,
    run_id: str,
    before_seq: int,
    limit: int = EVENT_TAIL,
    stage: str | None = None,
):
    load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    limit = max(1, min(limit, EVENT_PAGE_MAX))
    return page_events_before(project_id, run_id, before_seq, limit, stage)


@router.get("/project/{project_id}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, project_id: str, run_id: str):
    run_dir = resolve_run_dir(project_id, run_id)
    manifest = load_manifest(project_id, run_id)
    status_by_id = {s["stage_id"]: s.get("status", "") for s in manifest.get("stage_records", [])}
    graph = build_run_graph(project_id, manifest, status_by_id)

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            # The run view renders inside the project shell, so it carries the nav
            # state like every other section. `section: runs` keeps the Runs entry
            # highlighted while looking at one run.
            "state": shell_state(project_id, "runs"),
            "section": "runs",
            "crumbs": build_run_crumbs(project_id, run_id),
            "project": project_id,
            "run_id": run_id,
            "is_resuming": request.query_params.get("resuming") == "1",
            "manifest": manifest,
            "mermaid": graph.mermaid,
            "graph_error": graph.error,
            "event_tail": EVENT_TAIL,
            # The grounding line, the CTA and the stage strip — everything above
            # the graph (app.web.run_header).
            "header": build_run_header(project_id, run_id, run_dir, manifest),
            # What stopped this run, and what else its own records flagged — the
            # index above the graph (app.web.run_issues). Takes the stages the
            # graph already loaded, so the page reads the pinned version once.
            "issues": build_run_issues(manifest, graph.stages),
            # None when the pinned version carries no guide — the nav column is then
            # not rendered at all, rather than standing in for one with prose.
            "guide": build_run_guide_view(project_id, manifest),
            # The guide rail's stage chips resolve through the same links object
            # the stage panel uses, so the packet can point them at its own pages.
            "links": resolve_panel_links(project_id, run_id),
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get("/project/{project_id}/runs/{run_id}/artifact/{filename:path}")
async def run_artifact(project_id: str, run_id: str, filename: str):
    run_dir = resolve_run_dir(project_id, run_id)
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type, _ = mimetypes.guess_type(candidate.name)
    if media_type == "text/html":
        return HTMLResponse(content=candidate.read_text(encoding="utf-8"))
    return FileResponse(candidate, media_type=media_type or "application/octet-stream",
                        filename=candidate.name)


@router.post("/project/{project_id}/runs/{run_id}/resume")
async def resume_run_route(project_id: str, run_id: str):
    validate_project_or_404(project_id)
    load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    # Resume executes the version the run PINNED, so that snapshot is what has to
    # load — validating the live working copy here would block resuming a valid
    # run because of an unrelated edit. The seam loads it synchronously and only
    # then goes to a background thread (the re-run is LLM-heavy), so a bad
    # snapshot surfaces as a 400 here rather than dying where nothing reports it.
    try:
        run_service.resume(project_id, run_id)
    except RunVersionUnresolvableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "pinned workflow version failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}?resuming=1",
        status_code=303,
    )


@router.post("/project/{project_id}/runs/{run_id}/cancel")
async def cancel_run_route(project_id: str, run_id: str):
    manifest = load_manifest(project_id, run_id)  # 404s if the run doesn't exist
    if manifest.get("status") == RunStatus.RUNNING:
        request_cancel(project_id, run_id)
    return RedirectResponse(
        url=f"/project/{project_id}/runs/{run_id}",
        status_code=303,
    )
