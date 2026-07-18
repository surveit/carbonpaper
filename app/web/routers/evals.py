"""Eval read pages, framed by the project shell.

Three GET views, no authoring (that's the deferred form PR):

  GET /project/{project}/evals                      — the evals list (a shell section)
  GET /project/{project}/evals/{eval_id}            — one config: pathway, compatibility,
                                                       eval-dataset preview, scoring, run history
  GET /project/{project}/evals/{eval_id}/runs/{run} — one run's result

Structural twin of runs.py: the list is a section in project_shell (section_evals.html);
the two detail pages are standalone drill-downs (eval_detail.html / eval_run.html), each
with a back-link to the list. Configs belong to the project; a run pins the
workflow_version it scored. Nothing here writes — evals are authored elsewhere.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.errors import EvalNotScorableError
from app.core.models import EvalConfig, EvalRun, Stage
from app.evals.compatibility import check_eval_compatibility
from app.evals.runner import run_eval
from app.evals.store import (
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    load_eval_run,
)
from app.web.config import EXAMPLES_DIR, REPO_ROOT, templates
from app.web.loading import load_stages_or_empty, read_table
from app.web.project_view import shell_state

router = APIRouter()

# Rows rendered in the detail page's eval-dataset preview; the file may be larger.
DATASET_PREVIEW_ROWS = 50


# ─── The evals list (a project-shell section) ────────────────────────────────

@router.get("/project/{project}/evals", response_class=HTMLResponse)
async def evals_index(request: Request, project: str):
    """EVALS section of the project shell: one row per stored eval, each with its
    current status. Passes the SAME shell_state the other sections do (so the
    sidebar agrees) plus the status rows and any workflow-load issues that would
    stop compatibility from being checked at all."""
    project_dir = _resolve_project_dir(project)
    listing = load_stages_or_empty(project)
    return templates.TemplateResponse(
        request,
        "section_evals.html",
        {
            "state": shell_state(project_dir),
            "section": "evals",
            "evals": _build_eval_index_rows(project_dir, listing.stages),
            "load_issues": listing.issues,
        },
    )


def _build_eval_index_rows(project_dir: Path, stages: list[Stage]) -> list[dict[str, Any]]:
    """One `{id, name, status, issues}` row per eval config, in
    list_eval_configs order. A config that failed to validate shows as `broken`
    with its issues; one that loaded shows its computed status."""
    latest_version = latest_version_id(project_dir)
    rows: list[dict[str, Any]] = []
    for entry in list_eval_configs(project_dir):
        if entry.config is None:
            rows.append({"id": entry.id, "name": entry.id,
                         "status": "broken", "issues": entry.issues})
            continue
        status, run_issue = _resolve_eval_status(entry.config, stages, project_dir,
                                                  latest_version)
        rows.append({"id": entry.config.id, "name": entry.config.name,
                     "status": status, "issues": [run_issue] if run_issue else []})
    return rows


# ─── One config's detail ─────────────────────────────────────────────────────

@router.get("/project/{project}/evals/{eval_id}", response_class=HTMLResponse)
async def eval_detail(request: Request, project: str, eval_id: str):
    project_dir = _resolve_project_dir(project)
    config = _load_config_or_404(project_dir, eval_id)
    return _render_eval_detail(request, project, project_dir, config)


def _render_eval_detail(
    request: Request, project: str, project_dir: Path, config: EvalConfig
) -> HTMLResponse:
    """Assemble the detail page: the override→target pathway, whether the config
    still fits the workflow (compatibility), a read-only preview of the eval
    dataset if one is attached, the scoring rules, and the run history."""
    stages = load_stages_or_empty(project).stages
    report = check_eval_compatibility(config, stages)
    runs, runs_error = _list_eval_runs_safely(project_dir, config.id)
    latest_version = latest_version_id(project_dir)
    status = ("broken" if runs_error else
              eval_status(report, runs, latest_version,
                          has_eval_dataset=config.table is not None))
    executing = report.settings.frontier if report.settings is not None else []
    return templates.TemplateResponse(
        request,
        "eval_detail.html",
        {
            "project": project,
            "config": config,
            "report": report,
            "status": status,
            "executing": executing,
            "runs": runs,
            "runs_error": runs_error,
            **_read_eval_dataset_preview(config),
        },
    )


def _read_eval_dataset_preview(config: EvalConfig) -> dict[str, Any]:
    """Read-only preview of the attached eval dataset, capped at
    DATASET_PREVIEW_ROWS. Returns the declared columns (from the TableRef schema,
    always available) plus rows read from disk; a read failure lands in
    `dataset_error` so the page shows it instead of 500-ing."""
    if config.table is None:
        return {"has_eval_dataset": False, "dataset_columns": [], "dataset_rows": [],
                "dataset_error": None, "dataset_capped": False,
                "dataset_cap": DATASET_PREVIEW_ROWS}
    columns = [c.name for c in config.table.table_schema.columns]
    rows: list[dict[str, str]] = []
    error: str | None = None
    capped = False
    try:
        frame = read_table(REPO_ROOT / config.table.path)
        capped = len(frame) > DATASET_PREVIEW_ROWS
        preview = frame.head(DATASET_PREVIEW_ROWS).fillna("").astype(str).to_dict(orient="records")
        rows = [{str(k): v for k, v in row.items()} for row in preview]
    except (OSError, ValueError) as exc:
        error = str(exc)
    return {"has_eval_dataset": True, "dataset_columns": columns, "dataset_rows": rows,
            "dataset_error": error, "dataset_capped": capped,
            "dataset_cap": DATASET_PREVIEW_ROWS}


# ─── One run's result ────────────────────────────────────────────────────────

@router.get(
    "/project/{project}/evals/{eval_id}/runs/{run_id}",
    response_class=HTMLResponse,
)
async def eval_run_detail(request: Request, project: str, eval_id: str, run_id: str):
    project_dir = _resolve_project_dir(project)
    config = _load_config_or_404(project_dir, eval_id)
    try:
        run = load_eval_run(project_dir, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no run {run_id!r} for this eval") from exc
    except ValueError as exc:
        # The run file exists but can't be read — distinct from "not found".
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "eval_run.html",
        {"project": project, "config": config, "run": run},
    )


# ─── Trigger a run ───────────────────────────────────────────────────────────

@router.post("/project/{project}/evals/{eval_id}/run")
async def trigger_eval_run(project: str, eval_id: str):
    """Score the eval against the latest workflow version and redirect to the run.
    Synchronous: an eval that can't be run (incompatible, no dataset, or no version)
    surfaces as a 400 with the reason rather than a recorded non-result."""
    project_dir = _resolve_project_dir(project)
    config = _load_config_or_404(project_dir, eval_id)
    try:
        run = run_eval(project_dir, config, REPO_ROOT)
    except EvalNotScorableError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return RedirectResponse(
        url=f"/project/{project}/evals/{eval_id}/runs/{run.id}", status_code=303)


# ─── Shared helpers ──────────────────────────────────────────────────────────

def _resolve_project_dir(project: str) -> Path:
    """The project working copy under examples/<name>/, 404 if it isn't one."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return project_dir


def _load_config_or_404(project_dir: Path, eval_id: str) -> EvalConfig:
    """Load one config by id, or 404 if it's missing or unreadable."""
    try:
        return load_eval_config(project_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _resolve_eval_status(
    config: EvalConfig, stages: list[Stage], project_dir: Path, latest_version: str | None
) -> tuple[str, str | None]:
    """The one-word status for a loaded config, plus a run-listing error string
    if its `eval_run/` has a corrupt file (which forces `broken`)."""
    report = check_eval_compatibility(config, stages)
    runs, run_issue = _list_eval_runs_safely(project_dir, config.id)
    status = ("broken" if run_issue else
              eval_status(report, runs, latest_version,
                          has_eval_dataset=config.table is not None))
    return status, run_issue


def _list_eval_runs_safely(project_dir: Path, config_id: str) -> tuple[list[EvalRun], str | None]:
    """`list_eval_runs` raises loudly on a malformed `eval_run/*.json`; a page
    should still render, so return the error text instead of propagating."""
    try:
        return list_eval_runs(project_dir, config_id), None
    except (OSError, ValueError) as exc:
        return [], str(exc)
