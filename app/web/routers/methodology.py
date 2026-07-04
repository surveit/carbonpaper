"""Methodology browsing: the DAG view, per-stage detail (full page + partial),
the ER data-model view, and raw stage YAML."""

from __future__ import annotations

from typing import TypedDict

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.models import Stage
from app.services import node_review, versioning
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_er_diagram, build_mermaid_graph
from app.web.loading import (
    find_stage,
    list_methodologies,
    load_stages,
    read_prose_excerpt,
    resolve_function_code,
)
from app.web.routers.evals import EvalOverlayEntry, build_eval_overlay, uncovered_stages

router = APIRouter()


class StageEvalRow(TypedDict):
    """One eval in the stage page's "Evals touching this stage" list, tagged
    with the role it plays at that stage."""
    id: str
    name: str
    status: str
    url: str
    role: str


def _evals_for_stage(
    stage_id: str, eval_overlay: list[EvalOverlayEntry]
) -> list[StageEvalRow]:
    """Evals whose pathway includes `stage_id`, each tagged with the role it
    plays there. A stage can be both a reference override and among the
    executing stages of the same eval -- each eval contributes at most one
    row, using the most specific role: target, then overridden, then
    executing."""
    rows: list[StageEvalRow] = []
    for e in eval_overlay:
        if stage_id == e["target"]:
            role = "target"
        elif stage_id in e["overridden"]:
            role = "overridden"
        elif stage_id in e["executing"]:
            role = "executes"
        else:
            continue
        rows.append(StageEvalRow(id=e["id"], name=e["name"], status=e["status"],
                                 url=e["url"], role=role))
    return rows


def _eval_panel_context(
    methodology: str, stage_id: str, stages: list[Stage]
) -> dict[str, object]:
    """Context for the stage page's "Evals touching this stage" panel:
    `stage_evals` (rows tagged with role), and `evals_exist` (whether the
    methodology has any eval configs at all -- the panel is omitted entirely
    when it doesn't, rather than showing an empty-coverage warning)."""
    overlay = build_eval_overlay(methodology, EXAMPLES_DIR / methodology, stages)
    return {
        "stage_evals": _evals_for_stage(stage_id, overlay),
        "evals_exist": bool(overlay),
    }


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"methodologies": list_methodologies()},
    )


@router.get("/methodology/{methodology}", response_class=HTMLResponse)
async def methodology_view(request: Request, methodology: str):
    listing = load_stages(methodology)
    stages = listing.stages
    # Node-review layer: colour the DAG by belief (approved/unreviewed/rejected/
    # edited_stale) on first paint, drive the coverage badge, and list versions.
    # node_review speaks canonical spec dicts, so each typed Stage is dumped
    # back to its spec dict for hashing.
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    spec_dicts = [s.model_dump(by_alias=True, exclude_none=True) for s in stages]
    review_by_id = {
        s.id: node_review.approval_state_for(spec, decisions)["state"]
        for s, spec in zip(stages, spec_dicts)
    }
    coverage = node_review.coverage_for(spec_dicts, decisions)
    mermaid = build_mermaid_graph(stages, methodology, review_by_id=review_by_id)
    eval_overlay = build_eval_overlay(methodology, EXAMPLES_DIR / methodology, stages)
    uncovered = uncovered_stages(stages, eval_overlay)
    return templates.TemplateResponse(
        request,
        "methodology.html",
        {
            "methodology": methodology,
            "stages": stages,
            "mermaid": mermaid,
            "coverage": coverage,
            "versions": versioning.list_versions(EXAMPLES_DIR / methodology),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "load_issues": listing.issues,
            "order": listing.order,
            "eval_overlay": eval_overlay,
            "uncovered": uncovered,
        },
    )


@router.get("/methodology/{methodology}/stage/{stage_id}", response_class=HTMLResponse)
async def stage_view(request: Request, methodology: str, stage_id: str):
    listing = load_stages(methodology)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    prose = read_prose_excerpt(stage, methodology)
    function_code = resolve_function_code(stage)

    return templates.TemplateResponse(
        request,
        "stage.html",
        {
            "methodology": methodology,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_yaml": yaml.safe_dump(
                stage.model_dump(by_alias=True, exclude_none=True),
                sort_keys=False, allow_unicode=True,
            ),
            **_eval_panel_context(methodology, stage_id, listing.stages),
        },
    )


@router.get("/methodology/{methodology}/data-model", response_class=HTMLResponse)
async def data_model_view(request: Request, methodology: str):
    stages = load_stages(methodology).stages
    er = build_er_diagram(stages)
    return templates.TemplateResponse(
        request,
        "data_model.html",
        {
            "methodology": methodology,
            "stages": stages,
            "er_diagram": er,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.get("/methodology/{methodology}/stage/{stage_id}/partial", response_class=HTMLResponse)
async def stage_view_partial(request: Request, methodology: str, stage_id: str):
    """Stage detail content only — no <html> wrapper. Used by the split-view JS swap."""
    listing = load_stages(methodology)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    prose = read_prose_excerpt(stage, methodology)
    function_code = resolve_function_code(stage)
    return templates.TemplateResponse(
        request,
        "_stage_content.html",
        {
            "methodology": methodology,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_yaml": yaml.safe_dump(
                stage.model_dump(by_alias=True, exclude_none=True),
                sort_keys=False, allow_unicode=True,
            ),
            **_eval_panel_context(methodology, stage_id, listing.stages),
        },
    )


@router.get("/methodology/{methodology}/raw/{stage_id}", response_class=PlainTextResponse)
async def stage_raw_yaml(methodology: str, stage_id: str):
    stages = load_stages(methodology).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    return yaml.safe_dump(
        stage.model_dump(by_alias=True, exclude_none=True),
        sort_keys=False, allow_unicode=True,
    )
