"""The per-node panel: read one stage, edit its spec, (re)generate its tests.
Also owns the immutable version snapshots the runner pins runs to.
"""

from __future__ import annotations


from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.core.agent.store import MessageRole, PartType, open_session_store
from app.services import generation, stage_edit, versioning
from app.services import project as project_service
from app.services.errors import WorkflowLoadError
from app.services.loader import find_parsed_stage, list_parsed_stages, resolve_function_code
from app.models import stage_to_json
from app.web.config import templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import find_workflow_stage, load_stages
from app.web.eval_coverage import find_eval_coverages
from app.web.stage_test_views import build_certification, shape_test_views

from app.web.project_view import validate_project_or_404

router = APIRouter()


@router.get("/project/{project_id}/workflow/graph")
async def workflow_graph(project_id: str):
    stages = list_parsed_stages(load_stages(project_id).entries)
    return JSONResponse({"mermaid": build_mermaid_graph(stages, project_id)})


@router.get("/project/{project_id}/node/{stage_id}/panel", response_class=HTMLResponse)
async def node_panel(request: Request, project_id: str, stage_id: str):
    listing = load_stages(project_id)
    stage = find_parsed_stage(listing.entries, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project_id}")
    workflow = listing.workflow
    return templates.TemplateResponse(
        request,
        "_node_panel.html",
        {
            "project": project_id,
            "stage": stage,
            "workflow_stage": (workflow_stage := find_workflow_stage(workflow, stage_id)),
            "raw_json": stage_to_json(stage),
            "function_code": resolve_function_code(stage),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "test_views": (views := shape_test_views(workflow_stage)),
            "certification": build_certification(workflow_stage, views),
            # The working copy is not a version, so the freshest an eval can be here is
            # "it scored the latest version" — the same test eval_status applies.
            "eval_coverages": find_eval_coverages(
                project_id, stage_id, versioning.find_latest_version_id(project_id)),
            "can_generate_tests": stage.CARRIES_RUNNABLE_TESTS,
        },
    )


@router.post("/project/{project_id}/node/{stage_id}/edit")
async def node_edit(
    project_id: str,
    stage_id: str,
    spec_text: str = Form(...),
):
    validate_project_or_404(project_id)

    # `stage_edit.edit_stage_spec` parses, validates and writes; this route maps it onto HTTP.
    try:
        result = stage_edit.edit_stage_spec(stage_edit.open_working_copy(project_id), stage_id, spec_text)
    except FileNotFoundError as exc:
        # The project, or the stage's compiled file, is absent.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.ok:
        # Nothing was written — fail loudly with the issue list, never a partial write.
        return JSONResponse({"ok": False, "issues": result.issues}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/project/{project_id}/node/{stage_id}/generate-tests")
async def node_generate_tests(project_id: str, stage_id: str):
    """Replaces the stage's existing tests."""
    validate_project_or_404(project_id)
    model = project_service.project_meta(project_id).model or "sonnet"
    try:
        session_id = generation.start_stage_test_generation(
            project_id, stage_id=stage_id, model=model
        )
    except (ValueError, WorkflowLoadError) as exc:
        # ValueError: unknown/untestable stage, or a project with no document.
        # WorkflowLoadError: the compiled workflow itself does not load.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "session": session_id})


@router.get("/project/{project_id}/generation-session/{sid}/status")
async def generation_session_status(project_id: str, sid: str):
    del project_id  # URL-namespaced only; sessions are looked up by id, not by project.
    store = open_session_store()
    if not store.exists(sid):
        raise HTTPException(status_code=404, detail=f"No session '{sid}'")
    session = store.load(sid)
    # `active_turn` is truthy while the turn runs; a failure is only readable after.
    active = bool(session["active_turn"])
    error = None if active else _find_generation_failure(session["messages"])
    return JSONResponse({"active": active, "error": error})


def _find_generation_failure(messages: list[dict]) -> str | None:
    for message in messages:
        if message.get("role") != MessageRole.assistant:
            continue
        text = "".join(
            part.get("text", "") for part in message.get("parts", [])
            if part.get("type") == PartType.text
        )
        if text.startswith(generation.GENERATION_FAILURE_PREFIX):
            return text
    return None


# ─── Versioning ──────────────────────────────────────────────────────────────


@router.post("/project/{project_id}/version")
async def create_version_route(project_id: str, message: str = Form(...)):
    validate_project_or_404(project_id)

    # No compiler gate: a version is a snapshot of what the author has, and the
    # Workflow page already tells them what is wrong with it. A workflow that
    # published an artifact is one someone may want to pin whatever it is owed. The
    # only refusal left is a working copy that does not LOAD, which
    # save_working_copy_as_version raises below.
    try:
        version = project_service.save_working_copy_as_version(
            project_id,
            message=message,
            # The latest existing version — None for the very first one.
            parent_version=versioning.find_latest_version_id(project_id),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except WorkflowLoadError as exc:
        # save_working_copy_as_version validates the working copy first; hand its
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


