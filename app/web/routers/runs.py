"""Run lifecycle: trigger a run (against the latest published version, or a
specific pinned version), list runs, poll live status, render a run's
detail + per-stage panel, the scratch in-memory re-run, artifact serving, and
resume."""

from __future__ import annotations

import asyncio
import json
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.datastructures import FormData
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.concurrency import run_in_threadpool

from app.core.errors import (
    MissingInputBindingError,
    NoVersionToRunError,
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.core.run_status import RunStatus, StageStatus
from app.services.errors import WorkflowLoadError
from app.services.loader import resolve_function_code
from app.services.versioning import list_versions
from app.services import run as run_service
from app.services.run_guide import build_run_guide_view, find_guideless_version_id
from app.runtime.cancellation import request_cancel
from app.runtime.errors import PreviewError
from app.runtime.preview import PREVIEWABLE_TYPES, run_stage_preview
from app.runtime.run_log import RUN_DONE, read_events_since, read_events_window
from app.runtime.trace import trace_row, trace_to_dict
from app.runtime.trace_view import build_trace_view
from app.web.config import projects_dir, REPO_ROOT, templates
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import (
    build_llm_example,
    csv_download_body,
    list_file_inputs,
    save_uploaded_input,
    load_manifest,
    load_output_preview,
    load_output_row,
    load_output_table,
    manifest_stage,
    read_output_df,
    runs_dir,
)
from app.web.project_view import shell_state
from app.web.run_header import build_live_view, build_run_header
from app.web.run_index import build_run_index_rows
from app.web.run_stage_panel import not_executed_panel

router = APIRouter()

# How the run-log SSE tail polls events.jsonl, and how many empty polls it
# tolerates after the manifest has settled before it stops a stream whose
# run_done marker never arrived.
_EVENT_POLL_INTERVAL_S = 0.5
_IDLE_POLLS_BEFORE_TERMINAL_STOP = 2

# The run log's page size: how many events the SSE feed opens on, and how many
# one "load older" fetch brings back. The template reads EVENT_TAIL too, so the
# panel's "showing N of M" is sized by the same number the stream is.
EVENT_TAIL = 500
EVENT_PAGE_MAX = 5000


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
        # apply_run_bindings / prepare_run — not a catch-all for other bugs.
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "compiled workflow failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )


@router.post("/project/{project}/workflow/version/{version_id}/run")
async def trigger_run_of_version(project: str, version_id: str):
    """Run one specific version. Pins the run to `version_id`: prepare_run raises
    FileNotFoundError for a version_id with no version document on disk (404),
    NoVersionToRunError for a version_id that exists but is not published
    (400), and MissingInputBindingError/ValueError for a version whose stages
    aren't run-ready (e.g. an unbound file input) (400). Same
    background-and-redirect flow as trigger_run."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    try:
        run_id = run_service.start_run(project, version_id=version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except NoVersionToRunError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except (MissingInputBindingError, ValueError) as exc:
        # ValueError here is binding/limit/offset validation failures raised by
        # apply_run_bindings / prepare_run — not a catch-all for other bugs.
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except WorkflowLoadError as exc:
        return JSONResponse({"detail": "workflow version failed validation",
                             "issues": exc.issues}, status_code=400)
    return RedirectResponse(url=f"/project/{project}/runs/{run_id}",
                            status_code=303)


def _collect_bindings(
    form: FormData, project: str, version_id: str | None = None
) -> dict[str, dict[str, str]]:
    """Read `binding__<stage_id>` form fields into run bindings (each a
    connector-params dict, {"path": ...}). A field whose value equals the
    workflow-authored path is NOT a binding — the workflow is the designating
    source, and the manifest provenance should say so. `version_id` selects which
    version's authored paths to compare against (None -> latest), so a run pinned
    to an older version judges provenance against THAT version, not the latest."""
    authored = {fi["stage_id"]: fi["path"]
                for fi in list_file_inputs(project, version_id)}
    bindings: dict[str, dict[str, str]] = {}
    for key, value in form.items():
        if not key.startswith("binding__"):
            continue
        stage_id = key[len("binding__"):]
        path = str(value).strip()
        if path and path != authored.get(stage_id, ""):
            bindings[stage_id] = {"path": path}
    return bindings


def _collect_limits(form: FormData) -> dict[str, int]:
    """Read `limit__<stage_id>` form fields into a per-run row-cap override,
    the same shape `prepare_run`'s `limits` parameter takes. A blank field
    means "no cap" and is left out of the dict (never recorded as 0). A value
    that is not a non-negative whole number fails loudly, naming the stage."""
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
    """Whether the run form asked for a recompute-everything run, the shape
    `prepare_run`'s `bust_cache` parameter takes. A checkbox is submitted only
    when checked, so its presence in the form IS the value — there is no
    unchecked value to parse."""
    return "bust_cache" in form


@router.get("/project/{project}/run-inputs")
async def run_inputs(project: str, version_id: str | None = None):
    """The file-kind input stages of one version as JSON ([{stage_id, name,
    path}]). The run form fetches this when the version dropdown changes so its
    path fields describe the version about to run — a different version can author
    different input stages/paths. `version_id` None resolves to the latest."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return JSONResponse(list_file_inputs(project, version_id))


@router.post("/project/{project}/upload-input")
async def upload_input(
    project: str,
    stage_id: str = Form(...),
    file: UploadFile = File(...),
):
    """Save a browser-uploaded run-input file and return its absolute path as
    JSON ({ok:true, path}). The run form's Browse… uses the browser's own native
    file dialog (works on every OS) — but a browser hands over only bytes, never
    a path, so we save those bytes server-side (uploads/<stage_id>/<name>) and
    hand back the saved copy's path for the field. The disk copy runs in a
    threadpool so a large upload doesn't stall the event loop."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    if not file.filename:
        return JSONResponse({"ok": False, "error": "no file provided"}, status_code=400)
    path = await run_in_threadpool(
        save_uploaded_input, project_dir, stage_id, file.filename, file.file
    )
    return JSONResponse({"ok": True, "path": str(path)})


@router.get("/project/{project}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, project: str):
    """RUNS section of the project shell: the runs list, framed by the sidebar. Passes
    the SAME project_state the other sections do (so the sidebar / next-action agree)
    plus the manifest-backed run rows. 404 if the project dir doesn't exist."""
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
            "state": shell_state(pdir),
            "section": "runs",
            "runs": build_run_index_rows(project),
            # Only PUBLISHED versions are runnable (resolve_version_id gates on it),
            # so the run form's version picker offers only those — never an
            # unpublished version the run would then reject.
            "versions": [v for v in list_versions(pdir) if v.published],
            "file_inputs": list_file_inputs(project),
        },
    )


@router.get("/project/{project}/runs/{run_id}/status")
async def run_status(project: str, run_id: str):
    """Lightweight JSON for the live poller: current status, per-stage statuses,
    counts, and a freshly-built mermaid graph. Lets the run page update progress
    in place (no full-page reload) so it stays clickable while running."""
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
    """EITHER mermaid built from the version the run pinned, OR the reason that
    version could not be read — never both, and never a graph from elsewhere."""

    mermaid: str
    error: str | None


def build_run_graph(
    project: str, manifest: dict[str, Any], status_by_id: dict[str, str]
) -> RunGraph:
    try:
        stages = run_service.load_run_stages(project, manifest)
    except RunVersionUnresolvableError as exc:
        return RunGraph(mermaid="", error=str(exc))
    return RunGraph(
        mermaid=build_mermaid_graph(stages, project, status_by_id=status_by_id),
        error=None,
    )


@router.get("/project/{project}/runs/{run_id}/events")
async def stream_run_events(
    project: str,
    run_id: str,
    request: Request,
    from_seq: int | None = None,
    tail: int = EVENT_TAIL,
):
    """SSE tail of this run's event log, live or finished.

    Defaults to the LAST `tail` events, not the whole log: a row-per-event log
    of a 135k-row stage runs to 270k events, and streaming all of them is a feed
    no one can read arriving faster than a browser can render it. Older events
    are a page fetch away (/events/page); `from_seq` still replays from an exact
    cursor, which is what a reconnect uses.
    """
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    start = (
        _tail_start_seq(run_dir / "events.jsonl", tail)
        if from_seq is None
        else max(from_seq, 0)
    )
    return StreamingResponse(
        _tail_run_events(run_dir, request, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _tail_start_seq(events_path: Path, tail: int) -> int:
    """The seq to open a stream at so it yields the last `tail` events."""
    # One read of the log, and the answer comes off the parsed events rather than
    # from arithmetic on seq: taking `highest - tail` would assume seq has no
    # gaps, which is true of what the writer emits today but is not a property
    # the log itself carries.
    events = read_events_since(events_path, 0)
    if not events:
        return 0
    if tail <= 0:
        return int(events[-1]["seq"]) + 1      # start past the end: nothing old
    return 0 if len(events) <= tail else int(events[-tail]["seq"])


@router.get("/project/{project}/runs/{run_id}/events/page")
async def run_events_page(
    project: str, run_id: str, before_seq: int, limit: int = EVENT_TAIL
):
    """The page of events immediately BEFORE `before_seq` — "load older"."""
    # Backwards paging only. Newer events arrive on the SSE feed, so a forwards
    # page would be a second route to the same events with its own cursor to
    # keep in step; there is nothing for it to do.
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    limit = max(1, min(limit, EVENT_PAGE_MAX))
    start = max(0, before_seq - limit)
    events = read_events_window(
        run_dir / "events.jsonl", start, limit=max(0, before_seq - start)
    )
    return {"events": events, "first_seq": start, "has_more": start > 0}


async def _tail_run_events(
    run_dir: Path, request: Request, from_seq: int
) -> AsyncIterator[str]:
    """Drain runs/<id>/events.jsonl as it grows, ending on the run_done marker."""
    # The same generator serves a FINISHED run: it drains the file and ends, so
    # the live feed and after-the-fact investigation are one code path.
    # `from_seq` resumes after a reconnect (every event carries a monotonic seq).
    # File-tailing rather than asyncio wakeups is deliberate: the run and its LLM
    # rows execute on worker threads with no access to the server loop, and a
    # file crosses that boundary for free.
    events_path = run_dir / "events.jsonl"
    cursor = from_seq
    idle_polls = 0
    while True:
        if await request.is_disconnected():
            return
        new = read_events_since(events_path, cursor)
        for event in new:
            cursor = int(event["seq"]) + 1
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("kind") == RUN_DONE:
                return
        # Fallback stop: if the writer never wrote run_done (a crash mid-run),
        # end once the manifest has settled AND a couple of polls added nothing,
        # so a client never hangs on an interrupted run.
        if _find_terminal_status(run_dir) is not None:
            idle_polls = 0 if new else idle_polls + 1
            if idle_polls >= _IDLE_POLLS_BEFORE_TERMINAL_STOP:
                yield "event: done\ndata: {}\n\n"
                return
        await asyncio.sleep(_EVENT_POLL_INTERVAL_S)


def _find_terminal_status(run_dir: Path) -> str | None:
    """This run's settled status, or None while it is still running."""
    status = load_manifest(run_dir).get("status")
    return None if status == RunStatus.RUNNING else status


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
            "state": shell_state(projects_dir() / project),
            "section": "runs",
            "project": project,
            "run_id": run_id,
            "manifest": manifest,
            "mermaid": graph.mermaid,
            "graph_error": graph.error,
            "event_tail": EVENT_TAIL,
            # The grounding line, the CTA and the stage strip — everything above
            # the graph (app.web.run_header).
            "header": build_run_header(project, run_id, run_dir, manifest),
            # None when the pinned version carries no guide — the panel is then
            # not rendered at all, rather than standing in for one with prose.
            "guide": build_run_guide_view(project, manifest),
            # Set only when a guide could still be written for this run's version:
            # the version id the Generate-guide offer targets in the panel's place.
            "guideless_version": find_guideless_version_id(project, manifest),
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Per-run stage detail panel — status, validation, preview, error trace."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)
    stage_record = next(
        (s for s in manifest.get("stage_records", []) if s.get("stage_id") == stage_id),
        None,
    )

    # The panel's Schema tier and Transform detail describe what THIS run
    # executed, so they read the version it pinned. With no resolvable version
    # there is no stage definition to show and the panel says why.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    stage_def = pinned.stage
    if stage_record is None:
        # A stage the graph draws but this run never executed (a workflow test
        # injects its input stages) — see app.web.run_stage_panel.
        return not_executed_panel(request, project, run_id, manifest, stage_id, pinned)

    output_preview = load_output_preview(run_dir, stage_record.get("output_path"))
    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
    }
    input_previews: list[dict[str, Any]] = []
    if stage_def is not None:
        for input_id in stage_def.input_ids:
            input_previews.append(
                {
                    "id": input_id,
                    "preview": load_output_preview(run_dir, output_by_id.get(input_id)),
                }
            )

    function_code = resolve_function_code(stage_def)
    llm_example = build_llm_example(stage_def, input_previews) if stage_def else None

    return templates.TemplateResponse(
        request,
        "_run_stage_panel.html",
        {
            "project": project,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "stage_def_error": pinned.error,
            "preview": output_preview,
            "input_previews": input_previews,
            "function_code": function_code,
            "llm_example": llm_example,
            "test_views": (views := shape_test_views(stage_def)),
            "certification": build_certification(stage_def, views) if stage_def else None,
            "previewable": stage_def is not None and stage_def.type in PREVIEWABLE_TYPES,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/rows",
    response_class=HTMLResponse,
)
async def run_stage_rows(
    request: Request, project: str, run_id: str, stage_id: str
):
    """Full table of one stage's output, capped at MAX_TABLE_ROWS rendered rows.
    The page links to the uncapped CSV download."""
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    table = load_output_table(run_dir, stage_record.get("output_path"))
    return templates.TemplateResponse(
        request,
        "run_stage_rows.html",
        {
            "project": project,
            "run_id": run_id,
            "stage_id": stage_id,
            "stage": stage_record,
            "output_path": stage_record.get("output_path"),
            **table,
        },
    )


@router.get("/project/{project}/runs/{run_id}/stage/{stage_id}/rows.csv")
async def run_stage_rows_csv(project: str, run_id: str, stage_id: str):
    """One stage's complete output as a CSV download (no row cap)."""
    # UTF-8 behind a byte-order mark, so accented rows survive Excel on
    # Windows — `csv_download_body` carries the why.
    run_dir = runs_dir(project) / run_id
    stage_record = manifest_stage(run_dir, stage_id)
    df = read_output_df(run_dir, stage_record.get("output_path"))
    filename = f"{project}__{run_id}__{stage_id}.csv"
    return Response(
        content=csv_download_body(df),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/project/{project}/runs/{run_id}/stage/{stage_id}/lineage_panel",
    response_class=HTMLResponse,
)
async def run_stage_lineage_panel(
    request: Request, project: str, run_id: str, stage_id: str, row: int
):
    """Minimal stage view for the lineage page: the transform, the output
    schema, and the output trimmed to `row`. Reuses `_stage_executable.html`
    and `schema_table` — not the whole run-detail panel."""
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
    """Show-your-work for one output row: its ancestry through row-preserving
    stages, as JSON. 404 if the run/stage is absent, 400 if the row ordinal is
    out of range."""
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
    """The row's show-your-work as a read-only HTML page: a numbered story and a
    graph toggle on top; clicking a stage loads the row-trimmed panel below."""
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


@router.post("/project/{project}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, project: str, run_id: str, stage_id: str
):
    """SCRATCH in-memory re-run of one stage on a few selected input rows.

    Reads the chosen rows from this run's upstream outputs, runs the stage's
    handler in memory, and returns the output rows as JSON. Nothing is
    persisted: no manifest change, no output file, no artifact. Used by the
    node-detail panel's "Run transform on selected" button.

    Body (JSON): {"indices": [int, ...]}  — positional row indices into the
    stage's first upstream input.
    """
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    indices_raw = (body or {}).get("indices", [])
    indices: list[int] = []
    for i in indices_raw:
        try:
            indices.append(int(i))
        except (TypeError, ValueError):
            continue

    # This executes a stage against THIS run's rows and is read as "what that
    # stage did here", so it runs the version the run pinned. With no resolvable
    # version it refuses: executing the working copy would answer a question
    # nobody asked, under the label of this run.
    pinned = run_service.load_pinned_stage_def(project, manifest, stage_id)
    if pinned.error is not None:
        return JSONResponse({"ok": False, "error": pinned.error}, status_code=409)
    stage_def = pinned.stage
    if stage_def is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}'")

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stage_records", [])
    }

    try:
        result = run_stage_preview(
            stage_def=stage_def,
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            output_by_id=output_by_id,
            selected_indices=indices,
        )
    except PreviewError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface the real failure
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}",
             "traceback": traceback.format_exc(limit=8)},
            status_code=500,
        )

    return JSONResponse({"ok": True, **result})


@router.get("/project/{project}/runs/{run_id}/artifact/{filename:path}", response_class=HTMLResponse)
async def run_artifact(project: str, run_id: str, filename: str):
    """Serve generated HTML artifacts (per-org profiles etc.) inline."""
    run_dir = runs_dir(project) / run_id
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return HTMLResponse(content=candidate.read_text(encoding="utf-8"))


@router.post("/project/{project}/runs/{run_id}/resume")
async def resume_run_route(project: str, run_id: str):
    """Resume/continue a run from where it stopped, re-running any stage that is
    NOT already complete (so this serves BOTH: a halted run after its review
    decisions, AND an ERRORED run after the bug is fixed — it re-runs the failed
    stage + downstream and reuses completed upstream outputs)."""
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
    """Cooperative cancel: records a cancel request for (project, run_id) that
    the run thread polls at its checkpoints (see app.runtime.cancellation). A
    no-op on a run that is already terminal — cancelling only means something
    while the run is still `running` — but redirects back either way, same as
    resume, so the page's poller/reload handles the rest."""
    run_dir = runs_dir(project) / run_id
    manifest = load_manifest(run_dir)  # 404s if the run doesn't exist
    if manifest.get("status") == RunStatus.RUNNING:
        request_cancel(project, run_id)
    return RedirectResponse(
        url=f"/project/{project}/runs/{run_id}",
        status_code=303,
    )
