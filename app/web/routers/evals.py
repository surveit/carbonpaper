"""Eval read pages, framed by the project shell. Nothing here writes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app.core.errors import EvalNotScorableError
from app.core.frames import list_rows
from app.models import WorkflowNotFormed
from app.models.records.eval_config import EvalConfig
from app.models.records.eval_run import EvalRun
from app.evals.compatibility import CompatibilityReport, validate_eval_compatibility
from app.evals.dataset import read_table_ref
from app.evals.runner import start_eval_run
from app.evals.store import (
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    resolve_eval_result_path,
    load_eval_run,
)
from app.services.versioning import list_versions
from app.web.breadcrumbs import build_eval_crumbs, build_eval_run_crumbs
from app.web.config import templates
from app.web.config import EVENT_TAIL
from app.web.eval_run_view import (
    build_eval_rows,
    build_eval_run_rows,
    describe_eval_run_duration,
)
from app.web.loading import StageListing, load_stages_or_empty, render_frame_as_text
from app.web.project_view import shell_state, validate_project_or_404
from app.runtime.run_log import count_events
from app.web.run_events import (
    EVENT_PAGE_MAX,
    page_events_before,
    stream_events,
    tail_start_seq,
)

router = APIRouter()

# Rows rendered in the detail page's eval-dataset preview; the file may be larger.
DATASET_PREVIEW_ROWS = 50


# ─── The evals list (a project-shell section) ────────────────────────────────

@router.get("/project/{project_id}/evals", response_class=HTMLResponse)
def evals_index(request: Request, project_id: str):
    validate_project_or_404(project_id)
    listing = load_stages_or_empty(project_id)
    return templates.TemplateResponse(
        request,
        "section_evals.html",
        {
            "state": shell_state(project_id, "evals"),
            "section": "evals",
            "evals": _build_eval_index_rows(project_id, listing),
            "load_issues": listing.issues,
        },
    )


def _build_eval_index_rows(
    project_id: str, listing: StageListing
) -> list[dict[str, Any]]:
    latest_version = latest_version_id(project_id)
    rows: list[dict[str, Any]] = []
    for entry in list_eval_configs(project_id):
        if entry.config is None:
            rows.append({"id": entry.id, "name": entry.id,
                         "status": "broken", "issues": entry.issues})
            continue
        status, run_issue = _resolve_eval_status(entry.config, listing, project_id,
                                                  latest_version)
        rows.append({"id": entry.config.eval_id, "name": entry.config.name,
                     "status": status, "issues": [run_issue] if run_issue else []})
    return rows


# ─── One config's detail ─────────────────────────────────────────────────────

@router.get("/project/{project_id}/evals/{eval_id}", response_class=HTMLResponse)
def eval_detail(request: Request, project_id: str, eval_id: str):
    validate_project_or_404(project_id)
    config = _load_config_or_404(project_id, eval_id)
    return _render_eval_detail(request, project_id, config)


def _render_eval_detail(
    request: Request, project_id: str, config: EvalConfig
) -> HTMLResponse:
    listing = load_stages_or_empty(project_id)
    report = _report_compatibility(config, listing)
    runs, runs_error = _list_eval_runs_safely(project_id, config.eval_id)
    latest_version = latest_version_id(project_id)
    status = ("broken" if runs_error else
              eval_status(report, runs, latest_version,
                          has_eval_dataset=config.table is not None))
    executing = report.settings.frontier if report.settings is not None else []
    return templates.TemplateResponse(
        request,
        "eval_detail.html",
        {
            # One eval reads inside the project shell like one run does, with the
            # Evals nav entry still lit while looking at a config below it.
            "state": shell_state(project_id, "evals"),
            "section": "evals",
            "project": project_id,
            "crumbs": build_eval_crumbs(project_id, config_name=config.name),
            "config": config,
            "report": report,
            "status": status,
            "executing": executing,
            "runs": build_eval_run_rows(project_id, runs),
            "runs_error": runs_error,
            "versions": list_versions(project_id),
            **_read_eval_dataset_preview(config),
        },
    )


def _read_eval_dataset_preview(config: EvalConfig) -> dict[str, Any]:
    if config.table is None:
        return {"has_eval_dataset": False, "dataset_columns": [], "dataset_rows": [],
                "dataset_error": None, "dataset_capped": False,
                "dataset_cap": DATASET_PREVIEW_ROWS}
    columns = [c.name for c in config.table.table_schema.columns]
    rows: list[dict[str, str]] = []
    error: str | None = None
    capped = False
    try:
        frame = read_table_ref(config.table)
        capped = len(frame) > DATASET_PREVIEW_ROWS
        rows = list_rows(render_frame_as_text(frame.head(DATASET_PREVIEW_ROWS)))
    except (OSError, ValueError, EvalNotScorableError) as exc:
        error = str(exc)
    return {"has_eval_dataset": True, "dataset_columns": columns, "dataset_rows": rows,
            "dataset_error": error, "dataset_capped": capped,
            "dataset_cap": DATASET_PREVIEW_ROWS}


# ─── One run's result ────────────────────────────────────────────────────────

@router.get(
    "/project/{project_id}/evals/{eval_id}/runs/{run_id}",
    response_class=HTMLResponse,
)
def eval_run_detail(request: Request, project_id: str, eval_id: str, run_id: str):
    validate_project_or_404(project_id)
    config = _load_config_or_404(project_id, eval_id)
    run = _load_run_or_404(project_id, run_id)
    return templates.TemplateResponse(
        request,
        "eval_run.html",
        {
            "state": shell_state(project_id, "evals"),
            "section": "evals",
            "project": project_id,
            "crumbs": build_eval_run_crumbs(
                project_id, config_name=config.name, config_id=config.eval_id, run_id=run_id
            ),
            "config": config,
            "run": run,
            "elapsed": describe_eval_run_duration(run),
            # What the run compared, row by row. `result_ref` is absent on a
            # vetoed run and on one that errored before scoring; the pane then
            # states which of those it was rather than showing an empty table.
            "rows": (
                build_eval_rows(resolve_eval_result_path(project_id, run.result_ref), config.table)
                if run.result_ref else None
            ),
            "event_tail": EVENT_TAIL,
            # The subset run's own event log, tailed by the same panel the run
            # page uses. A run still executing gets the panel before it has logged
            # anything; a terminal run that wrote no log — a vetoed run executed
            # nothing — has none to offer.
            "log_href": (
                _eval_run_href(project_id, config.eval_id, run_id)
                if run.is_running() or count_events(project_id, run_id) else None
            ),
            "status_href": _eval_run_status_href(project_id, config.eval_id, run_id),
        },
    )


@router.get("/project/{project_id}/evals/{eval_id}/runs/{run_id}/status")
def eval_run_status(project_id: str, eval_id: str, run_id: str):
    """What moves while an eval run is in flight; the run page polls it and stops at `terminal`."""
    validate_project_or_404(project_id)
    run = _load_run_or_404(project_id, run_id)
    return JSONResponse({
        "status": run.status,
        "terminal": not run.is_running(),
        "elapsed": describe_eval_run_duration(run),
    })


# ─── That run's log, served to the same panel the run page uses ──────────────

@router.get("/project/{project_id}/evals/{eval_id}/runs/{run_id}/events")
def stream_eval_run_events(
    project_id: str,
    eval_id: str,
    run_id: str,
    request: Request,
    from_seq: int | None = None,
    tail: int = EVENT_TAIL,
    stage: str | None = None,
):
    _require_eval_run_log(project_id, run_id)
    start = (tail_start_seq(project_id, run_id, tail, stage) if from_seq is None
             else max(from_seq, 0))
    return StreamingResponse(
        stream_events(project_id, run_id, request, start, stage),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/project/{project_id}/evals/{eval_id}/runs/{run_id}/events/page")
def eval_run_events_page(
    project_id: str,
    eval_id: str,
    run_id: str,
    before_seq: int,
    limit: int = EVENT_TAIL,
    stage: str | None = None,
):
    _require_eval_run_log(project_id, run_id)
    return page_events_before(
        project_id, run_id, before_seq, max(1, min(limit, EVENT_PAGE_MAX)), stage)


def _require_eval_run_log(project_id: str, run_id: str) -> None:
    """404 when this eval run logged nothing — the same refusal the absent
    log gave."""
    validate_project_or_404(project_id)
    if not count_events(project_id, run_id):
        raise HTTPException(status_code=404, detail=f"no log for eval run {run_id!r}")


def _eval_run_href(project_id: str, eval_id: str, run_id: str) -> str:
    return f"/project/{project_id}/evals/{eval_id}/runs/{run_id}"


def _eval_run_status_href(project_id: str, eval_id: str, run_id: str) -> str:
    return _eval_run_href(project_id, eval_id, run_id) + "/status"


def _load_run_or_404(project_id: str, run_id: str) -> EvalRun:
    try:
        return load_eval_run(project_id, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for this eval") from exc
    except ValueError as exc:
        # The run file exists but can't be read — distinct from "not found".
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ─── Trigger a run ───────────────────────────────────────────────────────────

@router.post("/project/{project_id}/evals/{eval_id}/run")
async def trigger_eval_run(request: Request, project_id: str, eval_id: str):
    validate_project_or_404(project_id)
    config = _load_config_or_404(project_id, eval_id)
    form = await request.form()
    version_id = form.get("version_id") or None
    if version_id is not None and not isinstance(version_id, str):
        raise HTTPException(status_code=400, detail="version_id must be a string")
    try:
        run = start_eval_run(project_id, config, version_id=version_id)
    except EvalNotScorableError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except FileNotFoundError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return RedirectResponse(
        url=f"/project/{project_id}/evals/{eval_id}/runs/{run.run_id}", status_code=303)


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _load_config_or_404(project_id: str, eval_id: str) -> EvalConfig:
    try:
        return load_eval_config(project_id, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _report_compatibility(config: EvalConfig, listing: StageListing) -> CompatibilityReport:
    workflow = listing.workflow
    if isinstance(workflow, WorkflowNotFormed):
        return CompatibilityReport(ok=False, problems=[
            "cannot verify the path: the workflow has structural problems: "
            + "; ".join(workflow.issues)])
    return validate_eval_compatibility(config, workflow)


def _resolve_eval_status(
    config: EvalConfig, listing: StageListing, project_id: str,
    latest_version: str | None,
) -> tuple[str, str | None]:
    report = _report_compatibility(config, listing)
    runs, run_issue = _list_eval_runs_safely(project_id, config.eval_id)
    status = ("broken" if run_issue else
              eval_status(report, runs, latest_version,
                          has_eval_dataset=config.table is not None))
    return status, run_issue


def _list_eval_runs_safely(project_id: str, config_id: str) -> tuple[list[EvalRun], str | None]:
    """Swallowed so that one corrupt eval_run/*.json does not take the page down."""
    try:
        return list_eval_runs(project_id, config_id), None
    except (OSError, ValueError) as exc:
        return [], str(exc)
