# Route order matters: the literal /project/new is declared on THIS router BEFORE
# the /project/{project_id} section routes, so "new" is never captured as a project.
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from pydantic import ValidationError
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from app.core.utils import format_errors
from app.models import (
    NamedSchema,
    SchemaLibrary,
    Terms,
    build_workflow,
    find_workflow_compiler_warnings,
    stage_to_json,
    validate_named_schema,
)
from app.services import (
    code_approval, generation, methodology, project, terms, versioning,
)
from app.services.loader import list_parsed_stages, resolve_function_code
from app.services.workspace import LOADER_BOOKKEEPING_KEYS
from app.web.breadcrumbs import build_home_crumbs, build_version_crumbs
from app.web.config import templates
from app.runtime.stage_tests import run_stage_tests
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.diagrams import (
    SCHEMA_KIND_CLASS,
    SCHEMA_KIND_GLYPH,
    TYPE_CLASS,
    TYPE_GLYPH,
    build_mermaid_graph,
)
from app.web.loading import (
    find_workflow_stage,
    list_projects,
    load_stages_or_empty,
)
from app.web.project_view import shell_state, validate_project_or_404

router = APIRouter()



# ─── Home dashboard ──────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"projects": list_projects()},
    )


@router.post("/project/{project_name}/code-execution/withdraw")
async def withdraw_code_execution(project_name: str):
    """Only stops NEW python stages being written; ones already stored keep running."""
    code_approval.withdraw_code_execution_approval(validate_project_or_404(project_name))
    return RedirectResponse(f"/project/{project_name}", status_code=303)


@router.post("/project/{project_name}/private")
async def set_project_private(project_name: str, private: str = Form("")):
    """Stays on the project: a private one is gone from the home grid it would return to."""
    project_id = validate_project_or_404(project_name)
    project.set_project_private(project_id, private == "on")
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@router.post("/project/{project_name}/delete")
async def delete_project(project_name: str):
    project.delete_project(validate_project_or_404(project_name))
    return RedirectResponse("/", status_code=303)


# ─── New project (paste doc → project) ───────────────────────────────────────
# Create a project working copy directly under examples/<name>/. The DIRECTORY is the
# authoring session (no separate compilation id): the pasted document lands at
# examples/<name>/document.md and the chat transcript at chat.jsonl, so the gated
# authoring streams below key off the project NAME, not a comp id.
#
# DECLARED HERE, before the /project/{project_id} section routes below, so the literal
# /project/new is matched first (FastAPI matches in declaration order) — otherwise the
# {project_id} catch-all would capture "new" as a project name.


@router.get("/project/new", response_class=HTMLResponse)
async def new_project_form(request: Request):
    return templates.TemplateResponse(
        request,
        "compile_new_methodology.html",
        {
            "crumbs": build_home_crumbs("New project"),
            "default_name": "",
            "default_doc": "",
        },
    )


@router.post("/project/new")
async def new_project_submit(
    name: str = Form(...),
    doc_text: str = Form(...),
    model: str = Form("sonnet"),
):
    try:
        project_id = project.create_project(
            name, doc_text, model=model, source="pasted document").id
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    doc = methodology.read_methodology(project_id) or doc_text
    # Kick off data-model generation. It runs as a LIVE chat turn; land the user on it
    # so they watch the model being authored (it streams while it runs, then persists
    # as the session's transcript).
    session_id = generation.start_generation(project_id, document=doc, model=model)
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


@router.post("/project/{project_name}/generate")
async def generate_project(project_name: str):
    validate_project_or_404(project_name)
    document = methodology.read_methodology(project_name)
    if document is None:
        raise HTTPException(
            status_code=400,
            detail=f"project '{project_name}' has no methodology to generate from.",
        )
    model = project.project_meta(project_name).model or "sonnet"
    session_id = generation.start_generation(project_name, document=document, model=model)
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


# ─── Unified PROJECT sections ────────────────────────────────────────────────
# One project (examples/<name>/) is framed by a left-sidebar shell (project_shell)
# with Overview, the Workflow group, and the Documentation group. Each section route
# passes the SAME status snapshot (project_view.shell_state) plus its section name and
# the section-specific extras the matching section_*.html needs. The shell reads ONLY
# from `state`.


@router.get("/project/{project_name}", response_class=HTMLResponse)
async def project_overview(request: Request, project_name: str):
    validate_project_or_404(project_name)
    return templates.TemplateResponse(
        request,
        "section_overview.html",
        {"state": shell_state(project_name, "overview"), "section": "overview"},
    )


@router.get("/project/{project_name}/methodology", response_class=HTMLResponse)
async def project_methodology(request: Request, project_name: str):
    validate_project_or_404(project_name)
    state = shell_state(project_name, "methodology")
    document = methodology.read_methodology(project_name) or ""
    return templates.TemplateResponse(
        request,
        "section_methodology.html",
        {"state": state, "section": "methodology", "methodology": document},
    )


@router.get("/project/{project_name}/terms", response_class=HTMLResponse)
async def project_terms(request: Request, project_name: str):
    validate_project_or_404(project_name)
    # write_terms refuses a word carrying two meanings, so stored terms that will not
    # load were hand-edited — say which word, rather than 500 on the page that shows them.
    try:
        stored = terms.load_terms(project_name)
    except ValidationError as exc:
        stored, unreadable = None, format_errors(exc)
    else:
        unreadable = []
    return templates.TemplateResponse(
        request,
        "section_terms.html",
        {
            "state": shell_state(project_name, "terms"),
            "section": "terms",
            "terms": stored,
            "unreadable": "; ".join(unreadable),
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
        },
    )


@router.get("/project/{project_name}/workflow", response_class=HTMLResponse)
async def project_workflow(request: Request, project_name: str):
    validate_project_or_404(project_name)
    listing = load_stages_or_empty(project_name)
    parsed = list_parsed_stages(listing.entries)
    # A valid workflow draws off typed Stages; a broken/partial one falls back to the
    # raw draft dicts so its graph still renders with the holes visible.
    stages: list[Any] = parsed if parsed else project.load_stage_specs(project_name)
    mermaid = build_mermaid_graph(stages, project_name) if stages else None
    return templates.TemplateResponse(
        request,
        "section_workflow.html",
        {
            "state": shell_state(project_name, "workflow"),
            "section": "workflow",
            "stages": stages,
            "mermaid": mermaid,
            # From the TYPED stages only: warnings judge a valid workflow's quality,
            # and a workflow that does not load has its load issues shown instead.
            # The examples are RUN here — the whole suite is sub-second — so a stage
            # whose examples disagree with its code says so in the same list.
            # None, not an empty report, when nothing typed loaded: the template
            # renders an empty report as "nothing is wrong", which zero stages have
            # not earned.
            "compiler_warnings": find_workflow_compiler_warnings(
                parsed, run_stage_tests(parsed).count_failing_by_stage(),
            ) if parsed else None,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.get("/project/{project_name}/workflow/versions", response_class=HTMLResponse)
async def project_workflow_versions(request: Request, project_name: str):
    validate_project_or_404(project_name)
    versions = versioning.list_versions(project_name)
    return templates.TemplateResponse(
        request,
        "versions.html",
        {"state": shell_state(project_name, "versions"), "section": "versions", "versions": versions},
    )


@router.get("/project/{project_name}/versions")
async def versions_redirect(project_name: str):
    return RedirectResponse(
        url=f"/project/{project_name}/workflow/versions", status_code=307
    )


@router.get("/project/{project_name}/workflow/version/{version_id}",
            response_class=HTMLResponse)
async def project_workflow_version(request: Request, project_name: str, version_id: str):
    validate_project_or_404(project_name)
    try:
        version = versioning.load_version(project_name, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "version_detail.html",
        {
            "state": shell_state(project_name, "versions"),
            "section": "versions",
            "crumbs": build_version_crumbs(project_name, version_id),
            "version": version,
            "mermaid": build_mermaid_graph(version.stages, project_name),
        },
    )


@router.get(
    "/project/{project_name}/workflow/version/{version_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def version_stage_partial(
    request: Request, project_name: str, version_id: str, stage_id: str
):
    validate_project_or_404(project_name)
    try:
        version = versioning.load_version(project_name, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    stage = next((s for s in version.stages if s.id == stage_id), None)
    if stage is None:
        raise HTTPException(
            status_code=404, detail=f"No stage '{stage_id}' in version {version_id}"
        )
    return templates.TemplateResponse(
        request,
        "_version_stage.html",
        {
            "project": project_name,
            "version_id": version_id,
            "stage": stage,
            "workflow_stage": (
                workflow_stage := find_workflow_stage(
                    build_workflow(version.stages), stage_id)
            ),
            "raw_json": stage_to_json(stage),
            "function_code": resolve_function_code(stage),
            "test_views": (views := shape_test_views(workflow_stage)),
            "certification": build_certification(workflow_stage, views),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


# ─── Data-model review + schema edit (keyed on the PROJECT) ──────────────────
# Human review actions on an existing data model: approve the whole schema library
# (records reviewer trust in the whole schema set) and edit a single schema — both
# key off the project dir under examples/<name>/.


@router.post("/project/{project_name}/schema/{schema_name}/edit")
async def edit_schema(project_name: str, schema_name: str, json_text: str = Form(...)):
    validate_project_or_404(project_name)

    # Parse — a parse error is the reviewer's, surfaced as a 400 issue, file untouched.
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "issues": [f"JSON parse error: {exc}"]}, status_code=400)
    if not isinstance(parsed, dict):
        return JSONResponse(
            {"ok": False, "issues": ["edited schema must be a JSON object (a single schema dict)"]},
            status_code=400,
        )

    # Strip loader bookkeeping keys before validating/writing.
    schema = {k: v for k, v in parsed.items() if k not in LOADER_BOOKKEEPING_KEYS}

    # Guard: no renaming a schema via edit (no writing one file's content under
    # another's name). The path name is authoritative.
    parsed_name = schema.get("name")
    if parsed_name != schema_name:
        return JSONResponse(
            {"ok": False,
             "issues": [f"name in the edited JSON ('{parsed_name}') must equal the schema name '{schema_name}'"]},
            status_code=400,
        )

    issues = validate_named_schema(schema)
    if issues:
        # Refused — the write never happens, the file is unchanged.
        return JSONResponse({"ok": False, "issues": issues}, status_code=400)

    # Guard: the noun must ALREADY be one of the project's (edit revises; it does not
    # create — that's the compiler's job).
    stored = terms.load_terms(project_name)
    if schema_name not in {noun.name for noun in stored.nouns.schemas}:
        raise HTTPException(
            status_code=404,
            detail=f"'{project_name}' has no schema named '{schema_name}'",
        )
    terms.write_terms(project_name, Terms(
        nouns=SchemaLibrary(schemas=[
            NamedSchema.model_validate(schema) if noun.name == schema_name else noun
            for noun in stored.nouns.schemas
        ]),
        verbs=stored.verbs,
    ))
    return JSONResponse({"ok": True})
