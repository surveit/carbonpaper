"""Run lifecycle: trigger a run (against the latest published version, or a
specific pinned version), list runs, poll live status, render a run's detail,
serve its artifacts, resume and cancel. The per-stage panel, its row views and
the scratch re-run are app.web.routers.run_stage."""

from __future__ import annotations

import asyncio
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
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
from app.models import Stage
from app.models.stages.input_data import resolve_file_format
from app.services.errors import WorkflowLoadError
from app.services.versioning import list_versions
from app.services import run as run_service
from app.services.run_guide import build_run_guide_view
from app.runtime.cancellation import request_cancel
from app.runtime.run_log import RUN_DONE, read_events_since
from app.web.config import EVENT_TAIL, projects_dir, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import (
    list_file_inputs,
    save_uploaded_input,
    load_manifest,
    runs_dir,
)
from app.web.project_view import shell_state
from app.web.run_header import build_live_view, build_run_header
from app.web.run_index import build_run_index_rows
from app.web.run_issues import build_run_issues
from app.web.run_stage_panel import resolve_panel_links

router = APIRouter()

# How the run-log SSE tail polls events.jsonl, and how many empty polls it
# tolerates after the manifest has settled before it stops a stream whose
# run_done marker never arrived.
_EVENT_POLL_INTERVAL_S = 0.5
_IDLE_POLLS_BEFORE_TERMINAL_STOP = 2

# A ceiling on what one "load older" fetch may ask for; the default page size is
# EVENT_TAIL, in app.web.config, because the stage panel's log is sized by it too.
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
    connector-params dict, {"path": ..., "format": ...}). The format is the one
    the bound file's own extension designates: a binding merges OVER the authored
    params, so carrying only a path would leave a `.csv` to be read by the
    authored `format: parquet`. An extension no reader handles fails the trigger
    (400) rather than binding a file the run would misread.

    A field whose value equals the
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
            bindings[stage_id] = {"path": path,
                                  "format": resolve_file_format(path).value}
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
    """The file-kind input stages of one version as JSON ([{stage_id, path}]).
    The run form fetches this when the version dropdown changes so its
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
    """EITHER the stages of the version the run pinned and the mermaid built from
    them, OR the reason that version could not be read with `stages` None — never
    both, and never a graph or a stage list from elsewhere."""

    stages: list[Stage] | None
    mermaid: str
    error: str | None


def build_run_graph(
    project: str, manifest: dict[str, Any], status_by_id: dict[str, str]
) -> RunGraph:
    try:
        stages = run_service.load_run_stages(project, manifest)
    except RunVersionUnresolvableError as exc:
        return RunGraph(stages=None, mermaid="", error=str(exc))
    return RunGraph(
        stages=stages,
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
    stage: str | None = None,
):
    """SSE tail of this run's event log, live or finished.

    Defaults to the LAST `tail` events, not the whole log: a row-per-event log
    of a 135k-row stage runs to 270k events, and streaming all of them is a feed
    no one can read arriving faster than a browser can render it. Older events
    are a page fetch away (/events/page); `from_seq` still replays from an exact
    cursor, which is what a reconnect uses. `stage` narrows the feed to one
    stage's own events, which is what the stage panel's log opens on.
    """
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    start = (
        _tail_start_seq(run_dir / "events.jsonl", tail, stage)
        if from_seq is None
        else max(from_seq, 0)
    )
    return StreamingResponse(
        _tail_run_events(run_dir, request, start, stage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def select_stage_events(
    events: list[dict[str, Any]], stage: str | None
) -> list[dict[str, Any]]:
    """`events` narrowed to `stage`'s own, or all of them when stage is None."""
    if stage is None:
        return events
    # RUN_DONE rides through the filter: it is what ends an SSE stream, so a
    # scoped feed that dropped it would tail a finished run forever.
    return [
        event
        for event in events
        if event.get("stage") == stage or event.get("kind") == RUN_DONE
    ]


def _tail_start_seq(events_path: Path, tail: int, stage: str | None = None) -> int:
    """The seq to open a stream at so it yields the last `tail` events."""
    # One read of the log, and the answer comes off the parsed events rather than
    # from arithmetic on seq: taking `highest - tail` would assume seq has no
    # gaps, which is true of what the writer emits today but is not a property
    # the log itself carries. Under a stage filter the tail is counted over that
    # stage's events, so a stage buried in a 270k-event log still opens full.
    events = select_stage_events(read_events_since(events_path, 0), stage)
    if not events:
        return 0
    if tail <= 0:
        return int(events[-1]["seq"]) + 1      # start past the end: nothing old
    return 0 if len(events) <= tail else int(events[-tail]["seq"])


@router.get("/project/{project}/runs/{run_id}/events/page")
async def run_events_page(
    project: str,
    run_id: str,
    before_seq: int,
    limit: int = EVENT_TAIL,
    stage: str | None = None,
):
    """The page of events immediately BEFORE `before_seq` — "load older"."""
    # Backwards paging only. Newer events arrive on the SSE feed, so a forwards
    # page would be a second route to the same events with its own cursor to
    # keep in step; there is nothing for it to do.
    run_dir = runs_dir(project) / run_id
    load_manifest(run_dir)  # 404s if the run doesn't exist
    limit = max(1, min(limit, EVENT_PAGE_MAX))
    return _page_before(run_dir / "events.jsonl", before_seq, limit, stage)


def _page_before(
    events_path: Path, before_seq: int, limit: int, stage: str | None
) -> dict[str, Any]:
    """The last `limit` events older than `before_seq`, after the stage filter."""
    older = [
        event
        for event in select_stage_events(read_events_since(events_path, 0), stage)
        if int(event["seq"]) < before_seq
    ]
    # The window is cut AFTER filtering, not from `before_seq - limit`: a stage
    # holding a handful of events inside a 5000-seq span would otherwise hand
    # back a nearly empty page and report the rest as already loaded.
    page = older[-limit:]
    first_seq = int(page[0]["seq"]) if page else 0
    return {
        "events": page,
        "first_seq": first_seq,
        "has_more": len(older) > len(page),
    }


async def _tail_run_events(
    run_dir: Path, request: Request, from_seq: int, stage: str | None = None
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
        # The cursor clears the whole batch that was READ, not the subset the
        # stage filter yields: an event the filter drops must not come back on
        # the next poll.
        if new:
            cursor = int(new[-1]["seq"]) + 1
        for event in select_stage_events(new, stage):
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
    # A publish stage writes whatever its format says — an .xlsx workbook as
    # readily as an HTML profile — so decoding every artifact as text answers a
    # binary one with a UnicodeDecodeError.
    """Serve a run's artifact: HTML inline, anything else as its own file type."""
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
