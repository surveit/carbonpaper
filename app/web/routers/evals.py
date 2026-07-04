"""Read-only eval pages: the evals home for a methodology, a config's detail
page (pathway, compatibility problems, cases table, scoring rules, run
history), and a single run's detail page."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.models import EvalRun
from app.services.eval_compat import check_eval_compatibility
from app.services.eval_store import (
    eval_status,
    latest_version_id,
    list_eval_configs,
    list_eval_runs,
    load_eval_config,
    load_eval_run,
)
from app.services.table_check import read_table
from app.web.config import EXAMPLES_DIR, REPO_ROOT, templates
from app.web.loading import load_stages

router = APIRouter()

CASES_PREVIEW_ROWS = 50


def _list_eval_runs_safe(methodology_dir: Path, config_id: str) -> tuple[list[EvalRun], str | None]:
    """`list_eval_runs` raises loudly on a malformed `eval_run/*.json` file (by
    design — see eval_store.list_eval_runs). A page should still render: return
    the error text instead so the template can show it in a `.load-issues`
    block rather than a 500."""
    try:
        return list_eval_runs(methodology_dir, config_id), None
    except (OSError, ValueError) as exc:
        return [], str(exc)


@router.get("/methodology/{methodology}/evals", response_class=HTMLResponse)
async def evals_index(request: Request, methodology: str):
    listing = load_stages(methodology)
    methodology_dir = EXAMPLES_DIR / methodology
    entries = list_eval_configs(methodology_dir)
    latest_version = latest_version_id(methodology_dir)

    rows = []
    for entry in entries:
        eval_id = entry.path.stem
        if entry.config is None:
            rows.append({
                "id": eval_id,
                "name": eval_id,
                "status": "broken",
                "issues": entry.issues,
            })
            continue
        report = check_eval_compatibility(entry.config, listing.stages)
        runs, runs_error = _list_eval_runs_safe(methodology_dir, entry.config.id)
        status = "broken" if runs_error else eval_status(report, runs, latest_version)
        rows.append({
            "id": entry.config.id,
            "name": entry.config.name,
            "status": status,
            "issues": [runs_error] if runs_error else [],
        })

    return templates.TemplateResponse(
        request,
        "evals_index.html",
        {
            "methodology": methodology,
            "evals": rows,
            "load_issues": listing.issues,
        },
    )


@router.get("/methodology/{methodology}/evals/{eval_id}", response_class=HTMLResponse)
async def eval_detail(request: Request, methodology: str, eval_id: str):
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    listing = load_stages(methodology)
    report = check_eval_compatibility(config, listing.stages)
    runs, runs_error = _list_eval_runs_safe(methodology_dir, config.id)
    latest_version = latest_version_id(methodology_dir)
    status = "broken" if runs_error else eval_status(report, runs, latest_version)

    executing = report.settings.frontier if report.settings is not None else []

    cases_columns = [c.name for c in config.table.table_schema.columns]
    cases_rows: list[dict[str, Any]] = []
    cases_error: str | None = None
    cases_capped = False
    table_path = (REPO_ROOT / config.table.path)
    try:
        df = read_table(table_path, config.table.format)
        cases_capped = len(df) > CASES_PREVIEW_ROWS
        preview = df.head(CASES_PREVIEW_ROWS).fillna("").astype(str).to_dict(orient="records")
        cases_rows = [{str(k): v for k, v in row.items()} for row in preview]
    except (FileNotFoundError, ValueError) as exc:
        cases_error = str(exc)

    return templates.TemplateResponse(
        request,
        "eval_detail.html",
        {
            "methodology": methodology,
            "config": config,
            "report": report,
            "status": status,
            "executing": executing,
            "runs": runs,
            "runs_error": runs_error,
            "cases_columns": cases_columns,
            "cases_rows": cases_rows,
            "cases_error": cases_error,
            "cases_capped": cases_capped,
            "cases_cap": CASES_PREVIEW_ROWS,
        },
    )


@router.get(
    "/methodology/{methodology}/evals/{eval_id}/runs/{run_id}",
    response_class=HTMLResponse,
)
async def eval_run_detail(request: Request, methodology: str, eval_id: str, run_id: str):
    methodology_dir = EXAMPLES_DIR / methodology
    try:
        config = load_eval_config(methodology_dir, eval_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    try:
        run = load_eval_run(methodology_dir, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"no run {run_id!r} for this eval"
        ) from exc
    except ValueError as exc:
        # The requested run file itself exists but can't be read -- distinct
        # from "not found": say so explicitly rather than folding it into a
        # 404, and don't let it be confused with some other run being broken.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return templates.TemplateResponse(
        request,
        "eval_run.html",
        {
            "methodology": methodology,
            "config": config,
            "run": run,
        },
    )
