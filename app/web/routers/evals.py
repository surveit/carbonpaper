"""Read-only eval pages: the evals home for a methodology, a config's detail
page (pathway, compatibility problems, cases table, scoring rules, run
history), and a single run's detail page. `build_eval_overlay` assembles the
per-eval status/pathway summary shared by the evals home page and the
methodology page's workflow-graph overlay."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.models import EvalRun, Stage
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


class EvalOverlayEntry(TypedDict):
    """One eval's pathway summary, used both by the evals home table and the
    methodology page's workflow-graph overlay. `overridden` and `executing`
    are stage-id lists; `target` is a single stage id, or "" for a config that
    failed to parse (it has no resolvable pathway). `executing` is empty
    whenever compatibility couldn't derive run settings (unresolved stages, or
    the config itself is unreadable)."""
    id: str
    name: str
    status: str
    overridden: list[str]
    executing: list[str]
    target: str
    url: str


def _list_eval_runs_safe(methodology_dir: Path, config_id: str) -> tuple[list[EvalRun], str | None]:
    """`list_eval_runs` raises loudly on a malformed `eval_run/*.json` file (by
    design — see eval_store.list_eval_runs). A page should still render: return
    the error text instead so the template can show it in a `.load-issues`
    block rather than a 500."""
    try:
        return list_eval_runs(methodology_dir, config_id), None
    except (OSError, ValueError) as exc:
        return [], str(exc)


def _eval_overlay_with_issues(
    methodology: str, methodology_dir: Path, stages: list[Stage]
) -> list[tuple[EvalOverlayEntry, list[str]]]:
    """One (overlay entry, issues) pair per `eval_config/*.yaml` file. Issues
    are parse problems (unreadable YAML/schema) for a config that failed to
    load, or the run-listing error for one that loaded fine but whose
    `eval_run/` has a corrupt file — kept separate from `EvalOverlayEntry`
    because that shape is shared with the workflow-graph overlay, which has
    no use for free-text issue strings."""
    entries = list_eval_configs(methodology_dir)
    latest_version = latest_version_id(methodology_dir)
    out: list[tuple[EvalOverlayEntry, list[str]]] = []
    for entry in entries:
        eval_id = entry.path.stem
        url = f"/methodology/{methodology}/evals/{eval_id}"
        if entry.config is None:
            out.append((EvalOverlayEntry(
                id=eval_id, name=eval_id, status="broken",
                overridden=[], executing=[], target="", url=url,
            ), entry.issues))
            continue
        config = entry.config
        report = check_eval_compatibility(config, stages)
        runs, runs_error = _list_eval_runs_safe(methodology_dir, config.id)
        status = "broken" if runs_error else eval_status(report, runs, latest_version)
        executing = report.settings.frontier if report.settings is not None else []
        overridden = [config.override_stage,
                      *(ov.stage_id for ov in config.reference_overrides)]
        out.append((EvalOverlayEntry(
            id=config.id, name=config.name, status=status,
            overridden=overridden, executing=executing,
            target=config.target_stage, url=url,
        ), [runs_error] if runs_error else []))
    return out


def build_eval_overlay(
    methodology: str, methodology_dir: Path, stages: list[Stage]
) -> list[EvalOverlayEntry]:
    """One entry per `eval_config/*.yaml` file, in `list_eval_configs` order. A
    config that fails to parse gets `overridden=[]`, `executing=[]`,
    `target=""` — it still shows up (as `broken`) but contributes nothing to a
    workflow pathway since it has none."""
    return [entry for entry, _issues in
            _eval_overlay_with_issues(methodology, methodology_dir, stages)]


def uncovered_stages(stages: list[Stage], overlay: list[EvalOverlayEntry]) -> list[str]:
    """Stage ids on no eval's pathway (overridden, executing, or target on ANY
    eval). Empty when there are zero evals — that's its own empty state, not
    every stage being a warning."""
    if not overlay:
        return []
    covered: set[str] = set()
    for e in overlay:
        covered.update(e["overridden"])
        covered.update(e["executing"])
        if e["target"]:
            covered.add(e["target"])
    return [s.id for s in stages if s.id not in covered]


@router.get("/methodology/{methodology}/evals", response_class=HTMLResponse)
async def evals_index(request: Request, methodology: str):
    listing = load_stages(methodology)
    methodology_dir = EXAMPLES_DIR / methodology
    rows = [
        {**entry, "issues": issues}
        for entry, issues in
        _eval_overlay_with_issues(methodology, methodology_dir, listing.stages)
    ]

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
