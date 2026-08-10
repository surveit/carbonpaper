"""The per-node panel: read one stage, edit its spec, (re)generate its tests.
Also owns the immutable version snapshots the runner pins runs to.
"""

from __future__ import annotations


from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.core.agent.store import MessageRole, PartType, open_session_store
from app.services import generation, stage_edit, versioning
from app.services import project as project_service
from app.services.errors import WorkflowLoadError
from app.services import loader
from app.services.loader import resolve_function_code
from app.models import stage_to_json
from app.runtime.stage_tests import find_failing_stage_tests
from app.web.config import projects_dir, templates
from app.web.diagrams import TYPE_CLASS, TYPE_GLYPH, build_mermaid_graph
from app.web.loading import find_stage, load_stages
from app.web.stage_test_views import build_certification, shape_test_views

router = APIRouter()


@router.get("/project/{project}/workflow/graph")
async def workflow_graph(project: str):
    """Swapped in after a spec edit, which may have changed a stage's inputs."""
    stages = load_stages(project).stages
    return JSONResponse({"mermaid": build_mermaid_graph(stages, project)})


@router.get("/project/{project}/node/{stage_id}/panel", response_class=HTMLResponse)
async def node_panel(request: Request, project: str, stage_id: str):
    """Mirrors stage_view_partial, but its Spec tab is editable."""
    stages = load_stages(project).stages
    stage = find_stage(stages, stage_id)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {project}")
    return templates.TemplateResponse(
        request,
        "_node_panel.html",
        {
            "project": project,
            "stage": stage,
            "raw_json": stage_to_json(stage),
            "function_code": resolve_function_code(stage),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "test_views": (views := shape_test_views(stage)),
            "certification": build_certification(stage, views),
            "can_generate_tests": stage.CARRIES_RUNNABLE_TESTS,
        },
    )


@router.post("/project/{project}/node/{stage_id}/edit")
async def node_edit(
    project: str,
    stage_id: str,
    spec_text: str = Form(...),
):
    """The ONLY writer of the working copy from the web layer."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")

    # The parse/validate/write core is `stage_edit.edit_stage_spec`, shared with the
    # editing agent's `edit_stage` tool; this route only maps its result onto HTTP.
    try:
        result = stage_edit.edit_stage_spec(project, stage_id, spec_text)
    except FileNotFoundError as exc:
        # The project, or the stage, is absent.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not result.ok:
        # Nothing was written — fail loudly with the issue list, never a partial write.
        return JSONResponse({"ok": False, "issues": result.issues}, status_code=400)
    return JSONResponse({"ok": True})


@router.post("/project/{project}/node/{stage_id}/generate-tests")
async def node_generate_tests(project: str, stage_id: str):
    """Returns the session id the JS poller watches. REPLACES the stage's tests."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    model = project_service.project_meta(project_dir).model or "sonnet"
    try:
        session_id = generation.start_stage_test_generation(
            project_dir, stage_id=stage_id, model=model
        )
    except (ValueError, WorkflowLoadError) as exc:
        # ValueError: unknown/untestable stage, or a project with no document.
        # WorkflowLoadError: the compiled workflow itself does not load.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "session": session_id})


@router.get("/project/{project}/generation-session/{sid}/status")
async def generation_session_status(project: str, sid: str):
    """Poll target for a hidden generation session."""
    del project  # URL-namespaced only; sessions are looked up by id, not by project.
    store = open_session_store()
    if not store.exists(sid):
        raise HTTPException(status_code=404, detail=f"No session '{sid}'")
    session = store.load(sid)
    # `active_turn` is truthy while the turn runs; a failure is only readable after.
    active = bool(session["active_turn"])
    error = None if active else _find_generation_failure(session["messages"])
    return JSONResponse({"active": active, "error": error})


def _find_generation_failure(messages: list[dict]) -> str | None:
    """The text `persist_generation_failure` appended, or None if it never ran."""
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


@router.post("/project/{project}/version")
async def create_version_route(project: str, message: str = Form(...)):
    """Snapshot the working copy's stages + data model into a new version."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")

    # Tests are the stage's behavior contract: a version is a committable
    # snapshot, so it must not immortalise a python transform that fails its
    # own tests. Absent tests don't block — the gate holds existing
    # tests to green, it does not require them. The gate only applies when a
    # workflow exists; without one, save_working_copy_as_version's own
    # FileNotFoundError reports the missing workflow as a 400 below.
    if loader.exists(project):
        failing = find_failing_stage_tests(load_stages(project).stages)
        if failing:
            return JSONResponse({"ok": False, "issues": failing}, status_code=400)

    try:
        version = project_service.save_working_copy_as_version(
            project_dir,
            message=message,
            reviewer="local",
            # The latest existing version — None for the very first one.
            parent_version=versioning.find_latest_version_id(project_dir),
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


@router.post("/project/{project}/versions/{version_id}/publish")
async def publish_version_route(project: str, version_id: str):
    """Metadata only, idempotent: the gate a run pins to. Stage content is untouched."""
    project_dir = projects_dir() / project
    if not project_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project}'")
    try:
        versioning.publish_version(project_dir, version_id, reviewer="local")
    except FileNotFoundError as exc:
        # Also how a malformed version_id lands here — any shape but the timestamp
        # versioning.load_version expects finds no document.
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # Only ever posted from the version's own detail page, so go back there in one hop.
    return RedirectResponse(
        url=f"/project/{project}/workflow/version/{version_id}", status_code=303
    )
