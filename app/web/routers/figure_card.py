"""Its own router because app.web.routers.runs is at the import fan-out ceiling."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.models.citations import CitedValue
from app.models.claims import StageOutputCellCitation
from app.runtime.citations import read_citations
from app.web.breadcrumbs import build_run_child_crumbs
from app.web.config import templates
from app.web.figure_card import describe_figure_for_a_link_preview, load_figure_card
from app.web.loading import load_run_record
from app.web.project_view import shell_state

router = APIRouter()

_SECTION = "runs"


@router.get("/project/{project_id}/runs/{run_id}/figures", response_class=HTMLResponse)
async def published_figures(request: Request, project_id: str, run_id: str):
    """Every value this run's publish stages cited, each linking a card of its own."""
    load_run_record(project_id, run_id)  # 404s where the run does not exist
    cited = read_citations(project_id, run_id)
    return templates.TemplateResponse(
        request, "published_figures.html",
        {"project": project_id, "run_id": run_id, "figures": cited,
         "hrefs": [build_card_url(project_id, run_id, figure) for figure in cited],
         "state": shell_state(project_id, _SECTION), "section": _SECTION,
         "crumbs": build_run_child_crumbs(project_id, run_id, label="Figures")},
    )


@router.get("/figure/{project_id}/{run_id}/{stage_id}/{row}/{column}",
            response_class=HTMLResponse)
async def figure_card_page(request: Request, project_id: str, run_id: str,
                           stage_id: str, row: int, column: str):
    load_run_record(project_id, run_id)
    card = load_figure_card(project_id, run_id, _cite(run_id, stage_id, row, column))
    if card is None:
        raise HTTPException(
            status_code=404,
            detail=(f"No publish stage in run '{run_id}' cited {stage_id}.{column} "
                    f"row {row}, so this is a cell and not a published figure"),
        )
    return templates.TemplateResponse(
        request, "figure_card.html",
        {"card": card, "preview": describe_figure_for_a_link_preview(card),
         "page_url": str(request.url)},
    )


def build_card_url(project_id: str, run_id: str, figure: CitedValue) -> str:
    return (f"/figure/{quote(project_id, safe='')}/{quote(run_id, safe='')}"
            f"/{quote(figure.stage_id, safe='')}/{figure.row_ordinal}"
            f"/{quote(figure.column, safe='')}")


def _cite(run_id: str, stage_id: str, row: int, column: str) -> StageOutputCellCitation:
    # The card prints the value the publish stage recorded, so none is passed in.
    return StageOutputCellCitation(run_id=run_id, stage_id=stage_id, row_ordinal=row,
                                   column=column, value=None)
