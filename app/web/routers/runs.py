"""Run lifecycle: trigger a run (against the latest stored version, or a
specific pinned version), list runs, poll live status, render a run's detail,
serve its artifacts, resume and cancel. The per-stage panel, its row views and
the scratch re-run are app.web.routers.run_stage."""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.datastructures import FormData
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from app.core.errors import (
    MissingInputBindingError,
    NoVersionToRunError,
    RunVersionUnresolvableError,
)
from app.core.run_status import RunStatus, StageStatus
from app.models import WorkflowStage
from app.models.schema import StageId, TypeUnsafeUserStageConfigOverride
from app.models.stages.input_data import resolve_file_format
from app.services.errors import UploadTooLargeError, WorkflowLoadError
from app.services.versioning import list_versions
from app.services import run as run_service
from app.services.run_guide import build_run_guide_view
from app.services.uploads import max_upload_bytes, save_upload
from app.runtime.cancellation import request_cancel
from app.web.breadcrumbs import build_run_crumbs, build_runs_child_crumbs
from app.web.config import EVENT_TAIL, projects_dir, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import (
    list_file_inputs,
    load_manifest,
    runs_dir,
)
from app.web.project_view import shell_state
from app.web.run_events import (
    EVENT_PAGE_MAX,
    page_events_before,
    stream_events,
    tail_start_seq,
)
from app.web.run_header import build_live_view, build_run_header
from app.web.run_index import build_run_index_rows
from app.web.run_issues import build_run_issues
from app.web.run_stage_panel import resolve_panel_links

router = APIRouter()

@router.post("/project/{project}/run")
async def trigger_run(request: Request, project: str):
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    # _collect_bindings itself loads the version's stages (list_file_inputs), so
    # it can raise WorkflowLoadError for an unloadable snapshot just like
    # prepare_run below — both must land in the same 400 handling.
    try:
        form = await request.form()
        version_id = str(form.get("version_id") or "").strip() or None
        bindings = _collect_bindings(form, project, version_id)
        limits = _collect_limits(form)
        run_id = run_service.start_run(project, version_id=version_id,
                                       bindings=bindings, limits=limits,
                                       bust_cache=_read_bust_cache(form))
    except (NoVersionToRunError, MissingInputBindingError, ValueError) as exc:
        # ValueError here is binding/limit/offset validation failures raised by
        # _collect_bindings (an unreadable file extension), apply_run_bindings or
        # prepare_run — not a catch-all for other bugs.
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )


def _collect_bindings(
    form: FormData, project: str, version_id: str | None = None
) -> dict[StageId, TypeUnsafeUserStageConfigOverride]:
    """A binding merges OVER the authored params, so a path without its format keeps the
    wrong reader."""
    authored = {fi["stage_id"]: fi["path"]
                for fi in list_file_inputs(project, version_id)}
    bindings: dict[StageId, TypeUnsafeUserStageConfigOverride] = {}
    for key, value in form.items():
        if not key.startswith("binding__"):
            continue
        stage_id = key[len("binding__"):]
        path = str(value).strip()
        if path and path != authored.get(stage_id, ""):
            bindings[stage_id] = {"path": path,
                                  "format": resolve_file_format(path).value}
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


@router.get("/project/{project}/run-inputs")
async def run_inputs(project: str, version_id: str | None = None):
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return JSONResponse(list_file_inputs(project, version_id))


@router.post("/project/{project}/upload-input")
async def upload_input(project: str, file: UploadFile = File(...)):
    if not (projects_dir() / project).is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    if not file.filename:
        return JSONResponse({"ok": False, "error": "no file provided"}, status_code=400)
    # Off the event loop: the copy streams a file of any size to disk and hashes it.
    try:
        path = await run_in_threadpool(save_upload, project, file.filename, file.file)
    except UploadTooLargeError as exc:
        # The message names the limit and what to do; run_controls.js shows it verbatim.
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "path": str(path)})


@router.get("/project/{project}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, project: str):
    pdir = projects_dir() / project
    if not pdir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # A stored version that no longer validates raises WorkflowLoadError from
    # any listing/load (shell_state's version count included) and fails this
    # page loudly — the remedy is a store migration, not a tolerant render.
    return templates.TemplateResponse(
        request,
        "section_runs.html",
        {
            "state": shell_state(pdir, "runs"),
            "section": "runs",
            "runs": build_run_index_rows(project),
        },
    )


@router.get("/project/{project}/runs/new", response_class=HTMLResponse)
async def run_new(request: Request, project: str, version_id: str | None = None):
    pdir = projects_dir() / project
    if not pdir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    # Every stored version is runnable (resolve_version_id reads no publication
    # state), so the picker offers all of them newest-first. Registered ahead of
    # /runs/{run_id}, which would otherwise match "new" as a run id.
    versions = list_versions(pdir)
    # ?version_id= pre-picks one (the version page's "Run this version" sends it).
    # An id no version carries 404s rather than falling back to the latest, which
    # would launch a different workflow than the link named.
    if version_id is not None and not any(v.version_id == version_id for v in versions):
        raise HTTPException(status_code=404,
                            detail=f"No version '{version_id}' in project '{project}'")
    selected = version_id or (versions[0].version_id if versions else None)
    return templates.TemplateResponse(
        request,
        "section_run_new.html",
        {
            "state": shell_state(pdir, "runs"),
            "section": "runs",
            "crumbs": build_runs_child_crumbs(project, label="New run"),
            "versions": versions,
            "selected_version_id": selected,
            "file_inputs": list_file_inputs(project, selected),
            # So Browse… can refuse an oversized pick before spending the upload on it.
            "max_upload_bytes": max_upload_bytes(),
        },
    )


@router.get("/project/{project}/runs/{run_id}/status")
async def run_status(project: str, run_id: str):
    manifest = load_manifest(runs_dir(project) / run_id)
    mstages = manifest.get("stage_records", [])
    status_by_id = {s["stage_id"]: s.get("status", "") for s in mstages}
    graph = build_run_graph(project, manifest, status_by_id)

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
        "stages": [{"stage_id": s["stage_id"], "status": s.get("status")} for s in mstages],
        # The header parts that move while a run is in flight; the run page
        # updates them in place rather than fetching a second endpoint.
        "header": build_live_view(project, run_id, manifest).model_dump(),
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
    project: str, manifest: dict[str, Any], status_by_id: dict[str, str]
) -> RunGraph:
    try:
        workflow = run_service.load_run_workflow(project, manifest)
    except RunVersionUnresolvableError as exc:
        return RunGraph(stages=None, mermaid="", error=str(exc))
    workflow_stages = workflow.list_workflow_stages()
    return RunGraph(
        stages=workflow_stages,
        mermaid=build_mermaid_graph(
            [resolved.stage for resolved in workflow_stages],
            project, status_by_id=status_by_id),
        error=None,
    )


@router.get("/project/{project}/runs/{run_id}/events")
async def stream_run_events(
    project: str,
    run_id: str,
    request: Request,
    from_seq: int | None = None,
    tail: int = EVENT_TAIL,
    stage: str | None = None,
):
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    start = (
        tail_start_seq(run_dir / "events.jsonl", tail, stage)
        if from_seq is None
        else max(from_seq, 0)
    )
    return StreamingResponse(
        stream_events(run_dir, request, start, stage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/project/{project}/runs/{run_id}/events/page")
async def run_events_page(
    project: str,
    run_id: str,
    before_seq: int,
    limit: int = EVENT_TAIL,
    stage: str | None = None,
):
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    limit = max(1, min(limit, EVENT_PAGE_MAX))
    return page_events_before(run_dir / "events.jsonl", before_seq, limit, stage)


@router.get("/project/{project}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, project: str, run_id: str):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    status_by_id = {s["stage_id"]: s.get("status", "") for s in manifest.get("stage_records", [])}
    graph = build_run_graph(project, manifest, status_by_id)

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            # The run view renders inside the project shell, so it carries the nav
            # state like every other section. `section: runs` keeps the Runs entry
            # highlighted while looking at one run.
            "state": shell_state(projects_dir() / project, "runs"),
            "section": "runs",
            "crumbs": build_run_crumbs(project, run_id),
            "project": project,
            "run_id": run_id,
            "manifest": manifest,
            "mermaid": graph.mermaid,
            "graph_error": graph.error,
            "event_tail": EVENT_TAIL,
            # The grounding line, the CTA and the stage strip — everything above
            # the graph (app.web.run_header).
            "header": build_run_header(project, run_id, run_dir, manifest),
            # What stopped this run, and what else its own records flagged — the
            # index above the graph (app.web.run_issues). Takes the stages the
            # graph already loaded, so the page reads the pinned version once.
            "issues": build_run_issues(manifest, graph.stages),
            # None when the pinned version carries no guide — the nav column is then
            # not rendered at all, rather than standing in for one with prose.
            "guide": build_run_guide_view(project, manifest),
            # The guide rail's stage chips resolve through the same links object
            # the stage panel uses, so the packet can point them at its own pages.
            "links": resolve_panel_links(project, run_id),
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get("/project/{project}/runs/{run_id}/artifact/{filename:path}")
async def run_artifact(project: str, run_id: str, filename: str):
    run_dir = runs_dir(project) / run_id
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type, _ = mimetypes.guess_type(candidate.name)
    if media_type == "text/html":
        return HTMLResponse(content=candidate.read_text(encoding="utf-8"))
    return FileResponse(candidate, media_type=media_type or "application/octet-stream",
                        filename=candidate.name)


@router.post("/project/{project}/runs/{run_id}/resume")
async def resume_run_route(project: str, run_id: str):
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    run_dir = runs_dir(project) / run_id
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    # Resume executes the version the run PINNED, so that snapshot is what has to
    # load — validating the live working copy here would block resuming a valid
    # run because of an unrelated edit. The seam loads it synchronously and only
    # then goes to a background thread (the re-run is LLM-heavy), so a bad
    # snapshot surfaces as a 400 here rather than dying where nothing reports it.
    try:
        run_service.resume(project, run_id)
    except RunVersionUnresolvableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "pinned workflow version failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )


@router.post("/project/{project}/runs/{run_id}/cancel")
async def cancel_run_route(project: str, run_id: str):
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)  # 404s if the run doesn't exist
    if manifest.get("status") == RunStatus.RUNNING:
        request_cancel(project, run_id)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )
