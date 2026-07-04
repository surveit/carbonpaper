"""Methodology browsing: the DAG view, per-stage detail (full page + partial),
the ER data-model view, and raw stage JSON."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from app.services import node_review, versioning
from app.services.loader import stage_to_json, stage_to_spec_dict
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_er_diagram, build_mermaid_graph
from app.web.loading import (
    find_stage,
    list_methodologies,
    load_stages,
    read_prose_excerpt,
    resolve_function_code,
)

router = APIRouter()


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
    # node_review speaks canonical spec dicts (stage_to_spec_dict), which equal
    # json.loads of the persisted file — so a node's hash is the same whether it
    # came off disk or from a live Stage.
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    spec_dicts = [stage_to_spec_dict(s) for s in stages]
    review_by_id = {
        s.id: node_review.approval_state_for(spec, decisions)["state"]
        for s, spec in zip(stages, spec_dicts)
    }
    coverage = node_review.coverage_for(spec_dicts, decisions)
    mermaid = build_mermaid_graph(stages, methodology, review_by_id=review_by_id)
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
            "raw_json": stage_to_json(stage),
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
            "raw_json": stage_to_json(stage),
        },
    )


@router.get("/methodology/{methodology}/raw/{stage_id}")
async def stage_raw(methodology: str, stage_id: str) -> Response:
    listing = load_stages(methodology)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    return Response(
        content=stage_to_json(stage),
        media_type="application/json",
    )
