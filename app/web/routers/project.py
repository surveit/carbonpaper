"""Project browsing: the workflow view, per-stage detail (full page + partial),
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
    list_projects,
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
        {"projects": list_projects()},
    )


@router.get("/project/{project}", response_class=HTMLResponse)
async def project_view(request: Request, project: str):
    listing = load_stages(project)
    stages = listing.stages
    # Node-review layer: colour the workflow by belief (approved/unreviewed/rejected/
    # edited_stale) on first paint, drive the coverage badge, and list versions.
    # node_review speaks canonical spec dicts (stage_to_spec_dict), which equal
    # json.loads of the persisted file — so a node's hash is the same whether it
    # came off disk or from a live Stage.
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / project)
    spec_dicts = [stage_to_spec_dict(s) for s in stages]
    review_by_id = {
        s.id: node_review.approval_state_for(spec, decisions)["state"]
        for s, spec in zip(stages, spec_dicts)
    }
    coverage = node_review.coverage_for(spec_dicts, decisions)
    mermaid = build_mermaid_graph(stages, project, review_by_id=review_by_id)
    return templates.TemplateResponse(
        request,
        "project.html",
        {
            "project": project,
            "stages": stages,
            "mermaid": mermaid,
            "coverage": coverage,
            "versions": versioning.list_versions(EXAMPLES_DIR / project),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "load_issues": listing.issues,
            "order": listing.order,
        },
    )


@router.get("/project/{project}/stage/{stage_id}", response_class=HTMLResponse)
async def stage_view(request: Request, project: str, stage_id: str):
    listing = load_stages(project)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    prose = read_prose_excerpt(stage, project)
    function_code = resolve_function_code(stage)

    return templates.TemplateResponse(
        request,
        "stage.html",
        {
            "project": project,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_json": stage_to_json(stage),
        },
    )


@router.get("/project/{project}/data-model", response_class=HTMLResponse)
async def data_model_view(request: Request, project: str):
    stages = load_stages(project).stages
    er = build_er_diagram(stages)
    return templates.TemplateResponse(
        request,
        "data_model.html",
        {
            "project": project,
            "stages": stages,
            "er_diagram": er,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.get("/project/{project}/stage/{stage_id}/partial", response_class=HTMLResponse)
async def stage_view_partial(request: Request, project: str, stage_id: str):
    """Stage detail content only — no <html> wrapper. Used by the split-view JS swap."""
    listing = load_stages(project)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    prose = read_prose_excerpt(stage, project)
    function_code = resolve_function_code(stage)
    return templates.TemplateResponse(
        request,
        "_stage_content.html",
        {
            "project": project,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_json": stage_to_json(stage),
        },
    )


@router.get("/project/{project}/raw/{stage_id}")
async def stage_raw(project: str, stage_id: str) -> Response:
    listing = load_stages(project)
    stage = find_stage(listing.stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    return Response(
        content=stage_to_json(stage),
        media_type="application/json",
    )
