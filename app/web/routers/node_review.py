"""Node-level review + workflow versioning: the "node review" layer.

NODE review = "do we trust HOW this step is modeled?" — colours the workflow by a
content-hash approval state, and does NOT halt a run. (Distinct from the ROW
review queue in `review.py`, which is "is this run's DATA right?" and DOES halt a
run.) It mirrors the queue's decide/partial patterns, lifted from data rows up to
workflow node specs, and adds immutable version snapshots the runner pins runs to.

State lives under examples/<project>/: `node_decisions.parquet` (approvals)
and `versions/<id>/` (snapshots), managed by app.services.node_review + app.services.versioning.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.services import node_review, versioning
from app.services.loader import (
    find_stage_file,
    stage_to_json,
    stage_to_spec_dict,
    write_stage,
)
from app.models import Stage, parse_stage, validate_stage
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import find_stage, load_stages, resolve_function_code

router = APIRouter()


def _review_by_id(stages: list[Stage], decisions) -> dict[str, str]:
    """belief state per stage id (approved / unreviewed / rejected / edited_stale),
    the map build_mermaid_graph colours strokes by."""
    return {
        s.id: node_review.approval_state_for(stage_to_spec_dict(s), decisions)["state"]
        for s in stages
    }


@router.get("/project/{project}/review/status")
async def review_status(project: str):
    """Live poller for the project page: belief state per node, coverage, and a
    freshly-built mermaid graph coloured by approval. Mirrors run_status — the page
    swaps `mermaid` in place after a decision/edit so the workflow recolours without a
    full reload."""
    stages = load_stages(project).stages
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / project)
    review_by_id = _review_by_id(stages, decisions)
    coverage = node_review.coverage_for([stage_to_spec_dict(s) for s in stages], decisions)
    mermaid = build_mermaid_graph(stages, project, review_by_id=review_by_id)
    return JSONResponse({
        "review_by_id": review_by_id,
        "coverage": coverage,
        "mermaid": mermaid,
    })


@router.get(
    "/project/{project}/node/{stage_id}/review-partial",
    response_class=HTMLResponse,
)
async def node_review_partial(request: Request, project: str, stage_id: str):
    """Per-node REVIEW/EDIT panel (right side of the project split view). Mirrors
    stage_view_partial, but answers the node-review question (approve / reject / edit
    the spec) instead of showing the read-only stage detail."""
    stages = load_stages(project).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / project)
    review = node_review.approval_state_for(stage_to_spec_dict(stage), decisions)
    return templates.TemplateResponse(
        request,
        "_node_review.html",
        {
            "project": project,
            "stage": stage,
            "review": review,
            "raw_json": stage_to_json(stage),
            "function_code": resolve_function_code(stage),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.post("/project/{project}/node/{stage_id}/decide")
async def node_decide(
    project: str,
    stage_id: str,
    content_hash: str = Form(...),
    decision: str = Form(...),
    note: str | None = Form(None),
):
    """Record a reviewer's belief decision against a node's content_hash. Mirrors
    queue_decide: validate the verb loudly, upsert by (stage_id, content_hash) via
    node_review.record_node_decision, and return the resulting approval state so the
    chip flips without a reload."""
    if decision not in ("approve", "reject", "needs_changes"):
        raise HTTPException(status_code=400, detail=f"unknown decision '{decision}'")
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")

    node_review.record_node_decision(
        project_dir,
        stage_id=stage_id,
        content_hash=content_hash,
        decision=decision,
        reviewer="local",
        note=(note or None),
    )

    # Recompute the state from the freshly-loaded store against the node's CURRENT
    # spec — the same source of truth the workflow colours by — so the returned chip and
    # the workflow agree. (record_node_decision stores 'needs_changes' verbatim, which
    # approval_state_for reports as 'unreviewed' for colouring.)
    stages = load_stages(project).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    decisions = node_review.load_node_decisions(project_dir)
    state = node_review.approval_state_for(stage_to_spec_dict(stage), decisions)["state"]
    return JSONResponse({"ok": True, "state": state})


@router.post("/project/{project}/node/{stage_id}/edit")
async def node_edit(
    project: str,
    stage_id: str,
    spec_text: str = Form(...),
):
    """The ONLY writer into compiled/. Parse the posted JSON, validate it with
    validate_stage, and — only if it's clean — write it back to compiled/<id>.json.
    On validation issues return 400 with the issue list and write NOTHING (fail
    loudly, never a silent partial write). Editing changes the spec's content hash,
    so an approved node auto-drops to edited_stale until re-approved; we return the
    new hash + state so the node flips live."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")

    # Parse the posted JSON. A parse error is the reviewer's, not ours — surface it
    # as a validation issue (400), file untouched.
    try:
        parsed = json.loads(spec_text)
    except json.JSONDecodeError as exc:
        return JSONResponse(
            {"ok": False, "issues": [f"JSON parse error: {exc}"]}, status_code=400
        )
    if not isinstance(parsed, dict):
        return JSONResponse(
            {"ok": False, "issues": ["edited spec must be a JSON object (a single stage)"]},
            status_code=400,
        )

    # Strip loader-injected bookkeeping keys before validating/writing — they are
    # not part of the spec (and the canonical hash ignores them anyway).
    stage = {k: v for k, v in parsed.items() if k not in node_review.CANONICAL_IGNORE_KEYS}

    # Guard: the parsed id must equal the path id (no renaming a node via edit, no
    # writing one file's content under another's name).
    parsed_id = stage.get("id")
    if parsed_id != stage_id:
        return JSONResponse(
            {"ok": False,
             "issues": [f"id in the edited spec ('{parsed_id}') must equal the node id '{stage_id}'"]},
            status_code=400,
        )

    issues = validate_stage(stage)
    if issues:
        # Refused — the write never happens, the file is unchanged.
        return JSONResponse({"ok": False, "issues": issues}, status_code=400)

    # Guard: the target file must ALREADY exist. The edit endpoint revises an
    # existing node; it does not create new compiled files (that's the compiler's
    # job). Find the on-disk file for this stage id via the same loader convention.
    target = find_stage_file(project_dir / "compiled", stage_id)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No existing compiled file for stage '{stage_id}' in {project}",
        )

    # Persist the VALIDATED stage in the canonical on-disk form (the same shape
    # the compiler writes and every read path hashes), not the reviewer's raw text.
    validated = parse_stage(stage)
    write_stage(target, validated)

    spec = stage_to_spec_dict(validated)
    new_hash = node_review.node_content_hash(spec)
    decisions = node_review.load_node_decisions(project_dir)
    state = node_review.approval_state_for(spec, decisions)["state"]
    return JSONResponse({"ok": True, "content_hash": new_hash, "state": state})


# ─── Versioning ──────────────────────────────────────────────────────────────


@router.post("/project/{project}/version")
async def create_version_route(project: str, message: str = Form(...)):
    """Snapshot the working copy's {compiled/, schemas/} into a new immutable
    version + freeze approval coverage at creation time. The parent is the latest
    existing version (None for the very first version). The JS redirects to the
    versions list on success."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    existing = versioning.list_versions(project_dir)  # newest-first
    parent = existing[0]["id"] if existing else None
    try:
        meta = versioning.create_version(
            project_dir, message=message, reviewer="local", parent_version=parent
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "version": meta})


@router.get("/project/{project}/versions", response_class=HTMLResponse)
async def versions_index(request: Request, project: str):
    """List every version of a project, newest-first, with frozen coverage."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "project": project,
            "versions": versioning.list_versions(project_dir),
        },
    )
