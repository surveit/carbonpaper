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
from app.services import generation, project, versioning
from app.services.loader import LOADER_BOOKKEEPING_KEYS, resolve_function_code
from app.web.breadcrumbs import build_home_crumbs, build_version_crumbs
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
    load_schemas,
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

def _schema_spec(schema: dict[str, Any]) -> dict[str, Any]:
    """One schema with loader bookkeeping (_filename/_order/_error) removed — the
    spec only. The schema model is `extra="forbid"`, so validation and the edit
    textarea must both see the spec, never the bookkeeping keys the loader injects."""
    return {k: v for k, v in schema.items() if k not in LOADER_BOOKKEEPING_KEYS}


def _schema_json_map(schemas: list[dict[str, Any]]) -> dict[str, str]:
    """name → JSON text for the per-schema edit textareas — the spec only (loader
    bookkeeping stripped), so it round-trips through the schema-edit writer cleanly."""
    out: dict[str, str] = {}
    for s in schemas:
        name = s.get("name")
        if not name:
            continue
        out[name] = json.dumps(_schema_spec(s), indent=2, ensure_ascii=False)
    return out


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
# Create a project working copy directly under examples/<name>/. The DIRECTORY is the
# authoring session (no separate compilation id): the pasted document lands at
# examples/<name>/document.md and the chat transcript at chat.jsonl, so the gated
# authoring streams below key off the project NAME, not a comp id.
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
    """Create the examples/<name>/ working copy + its project.json, persist the pasted
    document at document.md, then redirect to the project's data-model section where
    authoring starts. The directory IS the session — the data-model stream keys off the
    project name and reads document.md / writes chat.jsonl in here.

    Truthfulness: we write project.json (via project.write_project_meta) so a NEW
    project carries a real model + created_at (non-legacy); we never fabricate those
    for legacy dirs. A name clash fails LOUDLY (400) rather than clobbering existing
    data — the rename is the human's decision."""
    try:
        safe_name = project.create_project(name, doc_text, model=model, source="pasted document")
    except (ValueError, ProjectExistsError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    project_dir = projects_dir() / safe_name
    doc = (project_dir / "document.md").read_text(encoding="utf-8")
    # Kick off data-model generation. It runs as a LIVE chat turn; land the user on it
    # so they watch the model being authored (it streams while it runs, then persists
    # as the session's transcript).
    session_id = generation.start_generation(project_dir, document=doc, model=model)
    return RedirectResponse(url=f"/chat/{session_id}", status_code=303)


@router.post("/project/{project_name}/generate")
async def generate_project(project_name: str):
    """(Re)kick data-model generation for an EXISTING project — the manual counterpart
    to the auto-kick on create (for a legacy project that has a document but no data
    model, or to regenerate from scratch). Reads document.md + the project's model,
    starts the data-model phase as a LIVE chat turn, and redirects to that session so
    the run is watchable. 400 if there is no document to generate from."""
    pdir = _project_dir(project_name)
    document_path = pdir / "document.md"
    if not document_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"examples/{project_name}/ has no document.md to generate from.",
        )
    model = project.project_meta(pdir).model or "sonnet"
    session_id = generation.start_generation(
        pdir, document=document_path.read_text(encoding="utf-8"), model=model
    )
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
        {"state": shell_state(pdir, "overview"), "section": "overview"},
    )


@router.get("/project/{project_name}/document", response_class=HTMLResponse)
async def project_document(request: Request, project_name: str):
    """DOCUMENT — the source methodology document, read-only. The route reads the file
    server-side (the template never touches the filesystem); `document` is '' / None
    when the project has no document, and the template shows an empty state. The path
    line is state.document_path (absolute, truthful)."""
    pdir = _project_dir(project_name)
    state = shell_state(pdir, "document")
    document = ""
    if state.document_path:
        try:
            document = Path(state.document_path).read_text(encoding="utf-8")
        except OSError:
            # The path came from project_state probing the disk; if it vanished
            # between snapshot and read, show the empty state rather than 500.
            document = ""
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
    schemas = load_schemas(pdir)
    return templates.TemplateResponse(
        request,
        "section_data_model.html",
        {
            "state": shell_state(pdir, "data_model"),
            "section": "data_model",
            "schemas": schemas,
            "er_diagram": build_schema_er_diagram(schemas) if schemas else None,
            "table_graph": build_schema_table_graph(schemas) if schemas else None,
            "issues": validate_schema_library([_schema_spec(s) for s in schemas]) if schemas else [],
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
        list(listing.stages) if listing.stages else project._load_compiled_stages(pdir)
    )
    mermaid = build_mermaid_graph(stages, project_name) if stages else None
    return templates.TemplateResponse(
        request,
        "section_workflow.html",
        {
            "state": shell_state(pdir, "workflow"),
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
        {"state": shell_state(pdir, "versions"), "section": "versions", "versions": versions},
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
    version's metadata, publish state, and the actions that target it (run this
    version, and publish while it is unpublished). A version is immutable, so nothing here edits — belief
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
            "state": shell_state(pdir, "versions"),
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
    """The ONLY writer into examples/<name>/schemas/. Parse the posted JSON, validate
    it with validate_named_schema, and — only if clean — write it back to the schema's
    file. On validation issues return 400 with the issue list and write NOTHING (fail
    loudly, never a silent partial write)."""
    pdir = _project_dir(project_name)

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

    # Guard: the target file must ALREADY exist (edit revises; it does not create —
    # that's the compiler's job). Find it via the same loader convention.
    schemas_dir = pdir / "schemas"
    target: Path | None = None
    for schema_file in sorted(schemas_dir.glob("*.json")):
        try:
            doc = json.loads(schema_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict) and doc.get("name") == schema_name:
            target = schema_file
            break
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No existing schema file for '{schema_name}' in examples/{project_name}/schemas/",
        )

    target.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"ok": True})
