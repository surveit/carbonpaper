"""Node-level review + workflow versioning: the "node review" layer.

NODE review = "do we trust HOW this step is modeled?" — colours the workflow by a
content-hash approval state, and does NOT halt a run. (Distinct from the ROW
review queue in `review.py`, which is "is this run's DATA right?" and DOES halt a
run.) It mirrors the queue's decide/partial patterns, lifted from data rows up to
workflow node specs, and adds immutable version snapshots the runner pins runs to.

State: `node_decisions.parquet` (approvals) under examples/<project>/, and version
snapshots as documents in the store's `workflow_version` collection — managed by
app.services.node_review + app.services.versioning.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.agent.store import MessageRole, PartType, open_session_store
from app.services import generation, node_review, stage_edit, versioning
from app.services import project as project_service
from app.services.errors import WorkflowLoadError
from app.services.loader import stage_to_json, stage_to_spec_dict
from app.models import Stage
from app.models.stages.stage_tests import STAGE_TEST_TYPES, StageTest
from app.runtime.stage_tests import StageTestResult, find_failing_stage_tests, run_stage_tests
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
            "test_views": _shape_test_views(stage),
            "test_derivable": stage.type in STAGE_TEST_TYPES,
        },
    )


def _shape_test_views(stage: Stage) -> list[dict[str, Any]]:
    """Pair each authored test with its run result, shaped for
    _stage_tests.html ([] for stages without tests)."""
    if not stage.tests:
        return []
    results = run_stage_tests(stage)
    return [
        _shape_one_test(test, result)
        for test, result in zip(stage.tests, results)
    ]


def _shape_one_test(test: StageTest, result: StageTestResult) -> dict[str, Any]:
    return {
        "name": test.name,
        "description": test.description,
        "status": result.status,
        "message": result.message,
        "inputs": [
            {"stage_id": stage_id, "columns": _list_row_columns(rows), "rows": rows}
            for stage_id, rows in test.inputs.items()
        ],
        "expected": {"columns": _list_row_columns(test.expected), "rows": test.expected},
        "diffs": [
            {"row": diff.row, "column": diff.column,
             "expected": diff.expected, "actual": diff.actual}
            for diff in result.diffs
        ],
    }


def _list_row_columns(rows: list[dict[str, Any]]) -> list[str]:
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


@router.post("/project/{project}/node/{stage_id}/generate-tests")
async def node_generate_tests(project: str, stage_id: str):
    """Kick off hidden stage-test derivation for one python-transform stage and
    return the session id the JS poller watches. `generation.start_stage_test_generation`
    raises ValueError for an unknown/non-python stage or a project with no document, and
    WorkflowLoadError (via its `load_workflow` call) if the compiled workflow itself
    fails to load — both surface here as 400 with the underlying message; the button is
    destructive (REPLACES the stage's tests wholesale on completion), which is
    documented on the button's tooltip, not re-litigated here."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    model = project_service.project_meta(project_dir).model or "sonnet"
    try:
        session_id = generation.start_stage_test_generation(
            project_dir, stage_id=stage_id, model=model
        )
    except (ValueError, WorkflowLoadError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "session": session_id})


@router.get("/project/{project}/generation-session/{sid}/status")
async def generation_session_status(project: str, sid: str):
    """Poll target for a hidden derivation session: `active` mirrors the session's
    `active_turn` (truthy while the turn runs); once inactive, `error` reports the
    persisted failure text if `app.compiler.stage_tests._persist_derivation_failure`
    appended one (an assistant message starting `derivation failed: `), else None."""
    del project  # URL-namespaced only; sessions are looked up by id, not by project.
    store = open_session_store()
    if not store.exists(sid):
        raise HTTPException(status_code=404, detail=f"No session '{sid}'")
    session = store.load(sid)
    active = bool(session["active_turn"])
    error = None if active else _find_derivation_failure(session["messages"])
    return JSONResponse({"active": active, "error": error})


def _find_derivation_failure(messages: list[dict]) -> str | None:
    """The persisted derivation-failure text among a session's messages (see
    `_persist_derivation_failure`), or None if none of them report one."""
    for message in messages:
        if message.get("role") != MessageRole.assistant:
            continue
        text = "".join(
            part.get("text", "") for part in message.get("parts", [])
            if part.get("type") == PartType.text
        )
        if text.startswith("derivation failed: "):
            return text
    return None


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

    # Tests are the stage's behavior contract: a version is a committable
    # snapshot, so it must not immortalise a python transform that fails its
    # own tests. Absent tests don't block — the gate holds existing
    # tests to green, it does not require them. The gate only applies when a
    # compiled workflow exists; without one, versioning.create_version_from_disk's own
    # FileNotFoundError reports the missing workflow as a 400 below.
    if (project_dir / "compiled").is_dir():
        failing = find_failing_stage_tests(load_stages(project).stages)
        if failing:
            return JSONResponse({"ok": False, "issues": failing}, status_code=400)

    existing = versioning.list_versions(project_dir)  # newest-first
    parent = existing[0].version_id if existing else None
    try:
        version = versioning.create_version_from_disk(
            project_dir, message=message, reviewer="local", parent_version=parent
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkflowLoadError as exc:
        # create_version_from_disk validates the working copy first; hand its
        # itemized issue report to the save handler (which renders `issues`) as
        # a structured 400 — the same shape trigger_run uses — never a bare 500.
        return JSONResponse(
            {"ok": False,
             "detail": ("Cannot save a version: the current working copy failed "
                        "validation. Fix these stages and save again."),
             "issues": exc.issues},
            status_code=400,
        )
    return JSONResponse({"ok": True, "version": version.model_dump(mode="json")})


@router.post("/project/{project}/versions/{version_id}/publish")
async def publish_version_route(project: str, version_id: str):
    """Record human approval on one version (the gate runs pin to). Idempotent;
    metadata only — stage content is never touched. A malformed version_id (any
    shape but the timestamp versioning.load_version expects) 404s through
    that same FileNotFoundError. Publish is only ever posted from the version's own
    detail page, so redirect back there (now showing published) in one hop."""
    project_dir = EXAMPLES_DIR / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    try:
        versioning.publish_version(project_dir, version_id, reviewer="local")
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/project/{project}/workflow/version/{version_id}", status_code=303
    )
