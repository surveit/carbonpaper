# Route order matters: the literal /project/new is declared on THIS router BEFORE
# the /project/{project} section routes, so "new" is never captured as a project.
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from app.core.errors import ProjectExistsError
from app.models import (
    find_workflow_compiler_warnings,
    stage_to_json,
    validate_named_schema,
    validate_schema_library,
)
from app.models.named_schemas import NamedSchema
from app.services import data_model, generation, methodology, project, versioning
from app.services.loader import resolve_function_code
from app.web.config import projects_dir, templates
from app.runtime.stage_tests import run_stage_tests
from app.web.stage_test_views import build_certification, shape_test_views
from app.web.diagrams import (
    SCHEMA_KIND_CLASS,
    SCHEMA_KIND_GLYPH,
    SCHEMA_KIND_ORDER,
    TYPE_CLASS,
    TYPE_GLYPH,
    build_mermaid_graph,
    build_schema_er_diagram,
    build_schema_table_graph,
)
from app.web.loading import (
    list_projects,
    load_stages_or_empty,
)
from app.web.project_view import shell_state

router = APIRouter()


# ─── Path guard ──────────────────────────────────────────────────────────────

def _project_dir(project_name: str) -> Path:
    """Resolve a project dir and 404 if it isn't a real project working copy. Guards
    every section + authoring route: refuse anything that isn't a DIRECT child of
    examples/ (no traversal, no absolute path), so a name like '..%2f..' or one
    resolving outside examples/ can never read or delete anything here."""
    target = (projects_dir() / project_name).resolve()
    if target.parent != projects_dir().resolve() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return projects_dir() / project_name


# ─── Per-schema edit seed ─────────────────────────────────────────────────────

def _schema_json_map(schemas: list[NamedSchema]) -> dict[str, str]:
    """name → JSON text for the per-schema edit textareas, in the shape the
    schema-edit writer parses back."""
    return {
        s.name: json.dumps(s.model_dump(mode="json", exclude_none=True),
                           indent=2, ensure_ascii=False)
        for s in schemas
    }


# ─── Home dashboard ──────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """The home dashboard: every project as a card (name, what's authored, counts),
    plus create + per-card delete. Cards come from loading.list_projects (truthful
    on-disk counts)."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {"projects": list_projects()},
    )


@router.post("/project/{project_name}/delete")
async def delete_project(project_name: str):
    """DESTRUCTIVE — remove an entire project DIRECTORY (schemas, workflow, run
    outputs). The project's documents in the store — versions, node-review and
    row-review decisions — are not touched, so a project deleted here and
    re-created under the same name inherits them.
    Guarded via _project_dir: only a DIRECT child directory of examples/
    can be removed (so a traversal, an absolute path, or a name resolving outside
    examples/ can never delete anything here). Only reachable via POST (the
    dashboard's confirm()-gated form). Redirects to the dashboard (303) on success."""
    target = _project_dir(project_name)
    shutil.rmtree(target)
    return RedirectResponse("/", status_code=303)


# ─── New project (paste doc → project) ───────────────────────────────────────
# There is no separate compilation id: the pasted document is stored under the
# project NAME, which is what the gated authoring streams below key off.
#
# DECLARED HERE, before the /project/{project} section routes below, so the literal
# /project/new is matched first (FastAPI matches in declaration order) — otherwise the
# {project} catch-all would capture "new" as a project name.


@router.get("/project/new", response_class=HTMLResponse)
async def new_project_form(request: Request):
    """The project CREATE form — paste a methodology doc that lands in a new
    examples/<name>/ working copy. Authoring then proceeds in the project's own
    sections (data model → approve → workflow)."""
    return templates.TemplateResponse(
        request,
        "compile_new_methodology.html",
        {"default_name": "", "default_doc": ""},
    )


@router.post("/project/new")
async def new_project_submit(
    name: str = Form(...),
    doc_text: str = Form(...),
    model: str = Form("sonnet"),
):
    """Store the pasted methodology and the project's identity, then redirect to
    the data-model section where authoring starts.

    A name clash fails LOUDLY (400) rather than clobbering existing data — the
    rename is the human's decision."""
    try:
        safe_name = project.create_project(name, doc_text, model=model, source="pasted document")
    except (ValueError, ProjectExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # Kick off data-model generation. It runs as a LIVE chat turn; land the user on it
    # so they watch the model being authored (it streams while it runs, then persists
    # as the session's transcript).
    session_id = generation.start_generation(
        projects_dir() / safe_name,
        document=methodology.read_methodology(safe_name) or doc_text,
        model=model,
    )
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


@router.post("/project/{project_name}/generate")
async def generate_project(project_name: str):
    """(Re)kick data-model generation for an EXISTING project — the manual counterpart
    to the auto-kick on create (for a legacy project that has a document but no data
    model, or to regenerate from scratch). Reads the stored methodology + the
    project's model, starts the data-model phase as a LIVE chat turn, and redirects
    to that session so the run is watchable. 400 if there is no document."""
    pdir = _project_dir(project_name)
    document = methodology.read_methodology(project_name)
    if document is None:
        raise HTTPException(
            status_code=400,
            detail=f"project '{project_name}' has no methodology to generate from.",
        )
    model = project.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(pdir, document=document, model=model)
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


# ─── Unified PROJECT sections ────────────────────────────────────────────────
# One project (examples/<name>/) is framed by a left-sidebar shell (project_shell)
# with five sections — Overview / Document / Data model / Workflow / Runs. Each
# section route passes the SAME status snapshot (project_view.shell_state) plus its
# section name and the section-specific extras the matching section_*.html needs. The
# shell reads ONLY from `state`.


@router.get("/project/{project_name}", response_class=HTMLResponse)
async def project_overview(request: Request, project_name: str):
    """OVERVIEW — the project hub: identity (meta), the 5 status tiles, and the
    prominent "do this next" CTA. Renders ONLY from project_state (no section extras);
    unknown facts (a legacy model/date) show truthfully as 'unknown', never fabricated."""
    pdir = _project_dir(project_name)
    return templates.TemplateResponse(
        request,
        "section_overview.html",
        {"state": shell_state(pdir), "section": "overview"},
    )


@router.get("/project/{project_name}/document", response_class=HTMLResponse)
async def project_document(request: Request, project_name: str):
    """DOCUMENT — the source methodology, read-only. `document` is '' when the
    project has none, and the template shows an empty state."""
    pdir = _project_dir(project_name)
    state = shell_state(pdir)
    document = methodology.read_methodology(project_name) or ""
    return templates.TemplateResponse(
        request,
        "section_document.html",
        {"state": state, "section": "document", "document": document},
    )


@router.get("/project/{project_name}/data_model", response_class=HTMLResponse)
async def project_data_model(request: Request, project_name: str):
    """DATA MODEL — named-schema cards by kind + ER diagram + the per-schema edit +
    the authoring chat. The chat/edit actions POST to the
    /project/{name}/data-model/... routes below."""
    pdir = _project_dir(project_name)
    schemas = data_model.load_schemas(project_name)
    return templates.TemplateResponse(
        request,
        "section_data_model.html",
        {
            "state": shell_state(pdir),
            "section": "data_model",
            "schemas": schemas,
            "er_diagram": build_schema_er_diagram(schemas) if schemas else None,
            "table_graph": build_schema_table_graph(schemas) if schemas else None,
            "issues": validate_schema_library(
                [s.model_dump(mode="json", exclude_none=True) for s in schemas]
            ) if schemas else [],
            "schema_json": _schema_json_map(schemas),
            "kind_order": SCHEMA_KIND_ORDER,
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
        },
    )


@router.get("/project/{project_name}/workflow", response_class=HTMLResponse)
async def project_workflow(request: Request, project_name: str):
    """WORKFLOW — the typed-stage pipeline: the mermaid graph, the per-node panel, and
    the Build / Run / Create-version controls. The version list lives on its own
    /workflow/versions tab, linked from here. Always navigable; renders an empty state
    when no workflow is authored yet.

    A workflow with a broken stage still renders — as a draft graph off raw dicts — so
    the reviewer sees the holes. Empty (not a 404) when there is no workflow yet, so
    the empty page renders."""
    pdir = _project_dir(project_name)
    listing = load_stages_or_empty(project_name)
    # A valid workflow draws off typed Stages; a broken/partial one falls back to the
    # raw draft dicts so its graph still renders with the holes visible.
    stages: list[Any] = (
        list(listing.stages) if listing.stages else project.load_stage_specs(project_name)
    )
    mermaid = build_mermaid_graph(stages, project_name) if stages else None
    return templates.TemplateResponse(
        request,
        "section_workflow.html",
        {
            "state": shell_state(pdir),
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
                listing.stages,
                run_stage_tests(listing.stages).count_failing_by_stage(),
            ) if listing.stages else None,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@router.get("/project/{project_name}/workflow/versions", response_class=HTMLResponse)
async def project_workflow_versions(request: Request, project_name: str):
    """VERSIONS — the version history, newest-first. Each version links to its
    read-only detail; published state shows read-only here (publishing is an
    approval act that happens on the detail page, after looking at the version).
    The mutable working copy (edit + review + create-version) lives at /workflow; a
    project with no versions yet shows the right CTA (generate a workflow, or
    snapshot the working copy you already have)."""
    pdir = _project_dir(project_name)
    versions = versioning.list_versions(pdir)
    return templates.TemplateResponse(
        request,
        "versions.html",
        {"state": shell_state(pdir), "section": "versions", "versions": versions},
    )


@router.get("/project/{project_name}/versions")
async def versions_redirect(project_name: str):
    """The versions list moved to /workflow/versions; keep the old path working."""
    return RedirectResponse(
        url=f"/project/{project_name}/workflow/versions", status_code=307
    )


@router.get("/project/{project_name}/workflow/version/{version_id}",
            response_class=HTMLResponse)
async def project_workflow_version(request: Request, project_name: str, version_id: str):
    """A single workflow VERSION, read-only: its frozen stage graph plus the
    version's metadata, publish state, and the actions that target it (publish,
    run this version). A version is immutable, so nothing here edits — belief
    review lives on the working-copy editor. 404 if the version does not exist."""
    pdir = _project_dir(project_name)
    try:
        version = versioning.load_version(pdir, version_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return templates.TemplateResponse(
        request,
        "version_detail.html",
        {
            "state": shell_state(pdir),
            "section": "versions",
            "version": version,
            "version_guide": versioning.find_latest_review_guide(project_name, version_id),
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
    """One frozen stage of a version, read-only — the panel the version page's graph
    nodes open. Reads the SNAPSHOT's stages, not the working copy: the point of the
    version page is what was frozen, which may since have been edited or deleted."""
    pdir = _project_dir(project_name)
    try:
        version = versioning.load_version(pdir, version_id)
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
            "raw_json": stage_to_json(stage),
            "function_code": resolve_function_code(stage),
            "test_views": (views := shape_test_views(stage)),
            "certification": build_certification(stage, views),
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
    """The ONLY writer of a single schema. Parse the posted JSON, validate it with
    validate_named_schema, and — only if clean — store it back over the schema of
    that name. On validation issues return 400 with the issue list and store
    NOTHING (fail loudly, never a silent partial write)."""
    _project_dir(project_name)

    # Parse — a parse error is the reviewer's, surfaced as a 400 issue, nothing stored.
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "issues": [f"JSON parse error: {exc}"]}, status_code=400)
    if not isinstance(parsed, dict):
        return JSONResponse(
            {"ok": False, "issues": ["edited schema must be a JSON object (a single schema dict)"]},
            status_code=400,
        )

    # Guard: no renaming a schema via edit (no storing one schema's content under
    # another's name). The name in the route is authoritative.
    parsed_name = parsed.get("name")
    if parsed_name != schema_name:
        return JSONResponse(
            {"ok": False,
             "issues": [f"name in the edited JSON ('{parsed_name}') must equal the schema name '{schema_name}'"]},
            status_code=400,
        )

    issues = validate_named_schema(parsed)
    if issues:
        # Refused — the write never happens, the stored schema is unchanged.
        return JSONResponse({"ok": False, "issues": issues}, status_code=400)

    # Guard: the schema must ALREADY exist (edit revises; it does not create —
    # that's the compiler's job).
    try:
        data_model.write_schema(project_name, NamedSchema.model_validate(parsed))
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"No existing schema '{schema_name}' in project '{project_name}'",
        ) from exc
    return JSONResponse({"ok": True})
