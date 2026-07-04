"""Node-level review + DAG versioning: the "reviewable workflow" layer.

NODE review = "do we trust HOW this step is modeled?" — colours the DAG by a
content-hash approval state, and does NOT halt a run. (Distinct from the ROW
review queue in `review.py`, which is "is this run's DATA right?" and DOES halt a
run.) It mirrors the queue's decide/partial patterns, lifted from data rows up to
DAG node specs, and adds immutable version snapshots the runner pins runs to.

State lives under examples/<methodology>/: `node_decisions.parquet` (approvals)
and `versions/<id>/` (snapshots), managed by app.services.node_review + app.services.versioning.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.services import node_review, versioning
from app.models import Stage, validate_stage
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import find_stage, load_stages, resolve_function_code

router = APIRouter()


def _spec_dict(stage: Stage) -> dict:
    """The canonical spec dict node_review hashes — a typed Stage dumped back
    to its on-disk mapping (aliases restored, unset optionals omitted)."""
    return stage.model_dump(by_alias=True, exclude_none=True)


def _review_by_id(stages: list[Stage], decisions) -> dict[str, str]:
    """belief state per stage id (approved / unreviewed / rejected / edited_stale),
    the map build_mermaid_graph colours strokes by."""
    return {
        s.id: node_review.approval_state_for(_spec_dict(s), decisions)["state"]
        for s in stages
    }


@router.get("/methodology/{methodology}/review/status")
async def review_status(methodology: str):
    """Live poller for the methodology page: belief state per node, coverage, and a
    freshly-built mermaid graph coloured by approval. Mirrors run_status — the page
    swaps `mermaid` in place after a decision/edit so the DAG recolours without a
    full reload."""
    stages = load_stages(methodology).stages
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    review_by_id = _review_by_id(stages, decisions)
    coverage = node_review.coverage_for([_spec_dict(s) for s in stages], decisions)
    mermaid = build_mermaid_graph(stages, methodology, review_by_id=review_by_id)
    return JSONResponse({
        "review_by_id": review_by_id,
        "coverage": coverage,
        "mermaid": mermaid,
    })


@router.get(
    "/methodology/{methodology}/node/{stage_id}/review-partial",
    response_class=HTMLResponse,
)
async def node_review_partial(request: Request, methodology: str, stage_id: str):
    """Per-node REVIEW/EDIT panel (right side of the methodology split view). Mirrors
    stage_view_partial, but answers the node-review question (approve / reject / edit
    the spec) instead of showing the read-only stage detail."""
    stages = load_stages(methodology).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    review = node_review.approval_state_for(_spec_dict(stage), decisions)
    return templates.TemplateResponse(
        request,
        "_node_review.html",
        {
            "methodology": methodology,
            "stage": stage,
            "review": review,
            "raw_yaml": yaml.safe_dump(_spec_dict(stage), sort_keys=False, allow_unicode=True),
            "function_code": resolve_function_code(stage),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.post("/methodology/{methodology}/node/{stage_id}/decide")
async def node_decide(
    methodology: str,
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
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")

    node_review.record_node_decision(
        methodology_dir,
        stage_id=stage_id,
        content_hash=content_hash,
        decision=decision,
        reviewer="local",
        note=(note or None),
    )

    # Recompute the state from the freshly-loaded store against the node's CURRENT
    # spec — the same source of truth the DAG colours by — so the returned chip and
    # the DAG agree. (record_node_decision stores 'needs_changes' verbatim, which
    # approval_state_for reports as 'unreviewed' for colouring.)
    stages = load_stages(methodology).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    decisions = node_review.load_node_decisions(methodology_dir)
    state = node_review.approval_state_for(_spec_dict(stage), decisions)["state"]
    return JSONResponse({"ok": True, "state": state})


@router.post("/methodology/{methodology}/node/{stage_id}/edit")
async def node_edit(
    methodology: str,
    stage_id: str,
    yaml_text: str = Form(...),
):
    """The ONLY writer into compiled/. Parse the posted YAML, validate it with
    validate_stage, and — only if it's clean — write it back to compiled/<id>.yaml.
    On validation issues return 400 with the issue list and write NOTHING (fail
    loudly, never a silent partial write). Editing changes the spec's content hash,
    so an approved node auto-drops to edited_stale until re-approved; we return the
    new hash + state so the node flips live."""
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")

    # Parse the posted YAML. A parse error is the reviewer's, not ours — surface it
    # as a validation issue (400), file untouched.
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return JSONResponse(
            {"ok": False, "issues": [f"YAML parse error: {exc}"]}, status_code=400
        )
    if not isinstance(parsed, dict):
        return JSONResponse(
            {"ok": False, "issues": ["edited spec must be a YAML mapping (a single stage dict)"]},
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
             "issues": [f"id in the edited YAML ('{parsed_id}') must equal the node id '{stage_id}'"]},
            status_code=400,
        )

    issues = validate_stage(stage)
    if issues:
        # Refused — the write never happens, the file is unchanged.
        return JSONResponse({"ok": False, "issues": issues}, status_code=400)

    # Guard: the target file must ALREADY exist. The edit endpoint revises an
    # existing node; it does not create new compiled files (that's the compiler's
    # job). Find the on-disk file for this stage id via the same loader convention.
    compiled_dir = methodology_dir / "compiled"
    target: Path | None = None
    for yaml_file in sorted(compiled_dir.glob("*.yaml")):
        try:
            with yaml_file.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except yaml.YAMLError:
            continue
        if data.get("id") == stage_id:
            target = yaml_file
            break
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No existing compiled file for stage '{stage_id}' in {methodology}",
        )

    with target.open("w", encoding="utf-8") as f:
        yaml.safe_dump(stage, f, sort_keys=False, allow_unicode=True)

    # Hash the PARSED stage's spec dict, not the raw posted mapping — the same
    # convention every read path uses, so the returned hash matches what the
    # DAG recolour poll will compute from the file we just wrote.
    spec = _spec_dict(Stage.model_validate(stage))
    new_hash = node_review.node_content_hash(spec)
    decisions = node_review.load_node_decisions(methodology_dir)
    state = node_review.approval_state_for(spec, decisions)["state"]
    return JSONResponse({"ok": True, "content_hash": new_hash, "state": state})


# ─── Versioning ──────────────────────────────────────────────────────────────


@router.post("/methodology/{methodology}/version")
async def create_version_route(methodology: str, message: str = Form(...)):
    """Snapshot the working copy's {compiled/, schemas/} into a new immutable
    version + freeze approval coverage at creation time. The parent is the latest
    existing version (None for the very first version). The JS redirects to the
    versions list on success."""
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    existing = versioning.list_versions(methodology_dir)  # newest-first
    parent = existing[0]["id"] if existing else None
    try:
        meta = versioning.create_version(
            methodology_dir, message=message, reviewer="local", parent_version=parent
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "version": meta})


@router.get("/methodology/{methodology}/versions", response_class=HTMLResponse)
async def versions_index(request: Request, methodology: str):
    """List every version of a methodology, newest-first, with frozen coverage."""
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "methodology": methodology,
            "versions": versioning.list_versions(methodology_dir),
        },
    )
