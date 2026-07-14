"""Node-level review + workflow versioning: the "node review" layer.

NODE review = "do we trust HOW this step is modeled?" — colours the workflow by a
content-hash approval state, and does NOT halt a run. (Distinct from the ROW
review queue in `review.py`, which is "is this run's DATA right?" and DOES halt a
run.) It mirrors the queue's decide/partial patterns, lifted from data rows up to
workflow node specs, and adds immutable version snapshots the runner pins runs to.

State: `node_decisions.parquet` (approvals) under examples/<project>/, and version
snapshots as documents in the store's `version` collection — managed by
app.services.node_review + app.services.versioning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.services import node_review, stage_edit, versioning
from app.services.loader import stage_to_json, stage_to_spec_dict
from app.core.models import Stage
from app.core.models.stages.examples import StageExample
from app.runtime.examples import ExampleResult, find_failing_examples, run_stage_examples
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import find_stage, load_stages, resolve_function_code
from app.web.project_view import shell_state

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
            "example_views": _shape_example_views(stage),
        },
    )


def _shape_example_views(stage: Stage) -> list[dict[str, Any]]:
    """Pair each authored example with its run result, shaped for
    _stage_examples.html ([] for stages without examples)."""
    if not stage.examples:
        return []
    results = run_stage_examples(stage)
    return [
        _shape_one_example(example, result)
        for example, result in zip(stage.examples, results)
    ]


def _shape_one_example(example: StageExample, result: ExampleResult) -> dict[str, Any]:
    return {
        "name": example.name,
        "description": example.description,
        "status": result.status,
        "message": result.message,
        "inputs": [
            {"stage_id": stage_id, "columns": _row_columns(rows), "rows": rows}
            for stage_id, rows in example.inputs.items()
        ],
        "expected": {"columns": _row_columns(example.expected), "rows": example.expected},
        "diffs": [
            {"row": diff.row, "column": diff.column,
             "expected": diff.expected, "actual": diff.actual}
            for diff in result.diffs
        ],
    }


def _row_columns(rows: list[dict[str, Any]]) -> list[str]:
    """Column order for rendering: first-appearance order across the rows."""
    seen: dict[str, None] = {}
    for row in rows:
        for key in row:
            seen.setdefault(key)
    return list(seen)


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
    """The ONLY writer into compiled/. Delegates the parse/validate/write core to
    `stage_edit.edit_stage_spec` (shared with the editing agent's `edit_stage`
    tool) and maps its result onto this route's HTTP contract: 400 with the issue
    list and nothing written on any parse/validation problem (fail loudly, never a
    silent partial write); 404 if the project or the stage's compiled file is
    absent. Editing changes the spec's content hash, so an approved node
    auto-drops to edited_stale until re-approved; we return the new hash + state
    so the node flips live."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")

    try:
        result = stage_edit.edit_stage_spec(project_dir, stage_id, spec_text)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.ok:
        return JSONResponse({"ok": False, "issues": result.issues}, status_code=400)
    # The writer reports only success; re-derive the node's colour here from the
    # freshly-written stage, the same way review_status colours the workflow, so
    # the caller can flip the node live without a full reload.
    stage = find_stage(load_stages(project).stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    spec = stage_to_spec_dict(stage)
    content_hash = node_review.node_content_hash(spec)
    decisions = node_review.load_node_decisions(project_dir)
    state = node_review.approval_state_for(spec, decisions)["state"]
    return JSONResponse({"ok": True, "content_hash": content_hash, "state": state})


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

    # Examples are the stage's behavior contract: a version is a committable
    # snapshot, so it must not immortalise a python transform that fails its
    # own examples. Absent examples don't block — the gate holds existing
    # examples to green, it does not require them. The gate only applies when a
    # compiled workflow exists; without one, versioning.create_version's own
    # FileNotFoundError reports the missing workflow as a 400 below.
    if (project_dir / "compiled").is_dir():
        failing = find_failing_examples(load_stages(project).stages)
        if failing:
            return JSONResponse({"ok": False, "issues": failing}, status_code=400)

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
    """VERSIONS section of the project shell: every version newest-first, with frozen
    coverage. A child of the Workflow group, so it passes the SAME shell_state the
    other sections do (the sidebar agrees) plus its version rows."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    return templates.TemplateResponse(
        request,
        "versions.html",
        {
            "state": shell_state(project_dir),
            "section": "versions",
            "versions": versioning.list_versions(project_dir),
        },
    )
