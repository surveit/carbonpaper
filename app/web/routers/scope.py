"""Its own router because app.web.routers.runs is at the import fan-out ceiling."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.errors import (
    RowOutOfRange,
    RunVersionUnresolvableError,
    StageNotInRun,
)
from app.runtime.errors import MissingLineage
from app.core.json_types import JsonDict
from app.models.branch_analysis import BranchId
from app.models.claims import StageOutputCellCitation
from app.web import scope_view
from app.web.scope_drawing import draw_the_scope
from app.web.scope_payload import CutRows, ScopeMap, build_scope_map_for_cut
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.project_view import shell_state

router = APIRouter()

_SCOPE_PATH = "/project/{project_id}/runs/{run_id}/scope"
# Repeatable: `?expand=a&expand=b` resolves both merges. docs/branch-analysis.md
_EXPAND = Query(default=None)


@router.get(_SCOPE_PATH, response_class=HTMLResponse)
async def scope_page(request: Request, project_id: str, run_id: str,
                     stage: str, row: int, column: str,
                     expand: list[str] | None = _EXPAND):
    citation = _cite(run_id, stage, row, column)
    try:
        scope, cuts = scope_view.load_scope_map(
            project_id, run_id, citation, frozenset(expand or ()))
    except (StageNotInRun, RowOutOfRange, RunVersionUnresolvableError) as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from missing
    return templates.TemplateResponse(
        request, "scope_map.html",
        {
            "project": project_id, "run_id": run_id, "scope": scope,
            "unfed": scope_view.say_what_no_row_fed(scope),
            "payload": _payload(scope, cuts),
            **_shell(project_id, run_id),
        },
    )


@router.get(f"{_SCOPE_PATH}.json", response_class=JSONResponse)
async def scope_json(project_id: str, run_id: str, stage: str, row: int, column: str,
                     expand: list[str] | None = _EXPAND):
    citation = _cite(run_id, stage, row, column)
    try:
        scope, cuts = scope_view.load_scope_map(project_id, run_id, citation,
                                                frozenset(expand or ()))
    except (StageNotInRun, RowOutOfRange, RunVersionUnresolvableError) as missing:
        raise HTTPException(status_code=404, detail=str(missing)) from missing
    return JSONResponse(_payload(scope, cuts))


@router.get(f"{_SCOPE_PATH}/panel", response_class=HTMLResponse)
async def scope_panel(request: Request, project_id: str, run_id: str,
                      stage: str, row: int, column: str,
                      expand: list[str] | None = _EXPAND):
    """The same map, shell-less, for the frame the row lineage page holds it in."""
    citation = _cite(run_id, stage, row, column)
    try:
        scope, cuts = scope_view.load_scope_map(project_id, run_id, citation,
                                                frozenset(expand or ()))
    except (MissingLineage, StageNotInRun, RowOutOfRange,
            RunVersionUnresolvableError) as no_map:
        # A pane that 404s shows the reader a browser error page inside a tab.
        return templates.TemplateResponse(
            request, "_scope_panel.html",
            {"project": project_id, "run_id": run_id, "citation": citation,
             "reason": str(no_map)},
        )
    return templates.TemplateResponse(
        request, "_scope_panel.html",
        {
            "project": project_id, "run_id": run_id, "scope": scope,
            "citation": citation,
            "unfed": scope_view.say_what_no_row_fed(scope),
            "payload": _payload(scope, cuts),
        },
    )


@router.get(f"{_SCOPE_PATH}/rows", response_class=HTMLResponse)
async def scope_rows(request: Request, project_id: str, run_id: str,
                     stage: str, row: int, at: str):
    """One stage's own data table, cut to the rows the cited figure came through."""
    try:
        table = scope_view.load_the_rows_that_reached(project_id, run_id, stage, row, at)
    except (MissingLineage, StageNotInRun, RowOutOfRange,
            RunVersionUnresolvableError) as no_rows:
        return HTMLResponse(f"<p class='muted'>{no_rows}</p>")
    return templates.TemplateResponse(request, "_scope_rows.html", table)


def _cite(run_id: str, stage: str, row: int, column: str) -> StageOutputCellCitation:
    # The cell's value is read back from the frame, so the caller need not carry it.
    return StageOutputCellCitation(run_id=run_id, stage_id=stage, row_ordinal=row,
                                   column=column, value=None)


def _payload(scope: ScopeMap, cuts: dict[BranchId, CutRows]) -> JsonDict:
    drawn = scope.model_dump(mode="json")
    drawn["cuts"] = {branch: _draw_the_cut(scope, cut) for branch, cut in cuts.items()}
    # Both, because the switch between them changes the layout, not just the styling.
    drawn["drawn"] = draw_the_scope(scope, every_stage=False).model_dump(mode="json")
    drawn["drawn_every_stage"] = draw_the_scope(
        scope, every_stage=True).model_dump(mode="json")
    return drawn


def _draw_the_cut(scope: ScopeMap, cut: CutRows) -> JsonDict:
    """A cut is a page of its own, so it arrives drawn rather than shaped in the browser."""
    at = build_scope_map_for_cut(scope, cut)
    drawn = cut.model_dump(mode="json")
    # Inside a cut every column is drawn: what these rows DID is the question.
    drawn["drawn"] = draw_the_scope(at, every_stage=True).model_dump(mode="json")
    return drawn


def _shell(project_id: str, run_id: str) -> JsonDict:
    return {"state": shell_state(project_id, "runs"), "section": "runs",
            "crumbs": build_run_child_crumbs(project_id, run_id, label="Scope")}
