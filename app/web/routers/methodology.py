"""Methodology browsing: the DAG view, per-stage detail (full page + partial),
the ER data-model view, and raw stage YAML."""

from __future__ import annotations

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.services import node_review, versioning
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_er_diagram, build_mermaid_graph
from app.web.loading import (
    find_stage,
    get_input_ids,
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
    stages = load_stages(methodology)
    # Node-review layer: colour the DAG by belief (approved/unreviewed/rejected/
    # edited_stale) on first paint, drive the coverage badge, and list versions.
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    review_by_id = {
        s["id"]: node_review.approval_state_for(s, decisions)["state"]
        for s in stages if s.get("id")
    }
    coverage = node_review.coverage_for(stages, decisions)
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
            "get_input_ids": get_input_ids,
        },
    )


@router.get("/methodology/{methodology}/stage/{stage_id}", response_class=HTMLResponse)
async def stage_view(request: Request, methodology: str, stage_id: str):
    stages = load_stages(methodology)
    stage = find_stage(stages, stage_id)
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
            "raw_yaml": yaml.safe_dump(stage, sort_keys=False, allow_unicode=True),
        },
    )


@router.get("/methodology/{methodology}/data-model", response_class=HTMLResponse)
async def data_model_view(request: Request, methodology: str):
    stages = load_stages(methodology)
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
    stages = load_stages(methodology)
    stage = find_stage(stages, stage_id)
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
            "raw_yaml": yaml.safe_dump(stage, sort_keys=False, allow_unicode=True),
        },
    )


@router.get("/methodology/{methodology}/raw/{stage_id}", response_class=PlainTextResponse)
async def stage_raw_yaml(methodology: str, stage_id: str):
    stages = load_stages(methodology)
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    return yaml.safe_dump(stage, sort_keys=False, allow_unicode=True)
