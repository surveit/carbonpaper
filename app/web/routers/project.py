"""The project shell (Overview / Document / Data model / Workflow / Runs), plus
project create/delete and the data-model approval gate. The Runs section page
itself is served by app.web.routers.runs.
Route order matters: the literal /project/new is declared on THIS router BEFORE
the /project/{project} section routes, so "new" is never captured as a project.
"""

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
    validate_named_schema,
    validate_schema_library,
)
from app.services import generation, node_review, project, versioning
from app.services.loader import resolve_function_code, stage_to_json, stage_to_spec_dict
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


# ─── Gated-flow render helpers (the data-model gate + per-schema edit seed) ────

def _schema_library_approval(
    project_dir: Path, schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the data-model section's `approval` dict for the WHOLE schema library:
    {state, current_hash, matched_decision}.

    current_hash is the schema_library_content_hash (POSTed back on approve and the
    exact value approve_schema_library records), NOT a hash recomputed from a synthetic
    node — so the displayed hash, the approve POST, and the stored decision agree.
    matched_decision is the latest row that determined the state, so the template can
    attribute "(was) approved by <reviewer> at <ts>" — pulled from the SAME
    SCHEMA_LIBRARY_STAGE_ID rows data_model_state reads."""
    current_hash = node_review.schema_library_content_hash(schemas)
    df = node_review.load_node_decisions(project_dir)
    matched: dict[str, Any] | None = None
    if df is not None and not df.empty:
        lib_rows = df[df["stage_id"] == node_review.SCHEMA_LIBRARY_STAGE_ID]
        current_rows = lib_rows[lib_rows["content_hash"] == current_hash]
        if not current_rows.empty:
            matched = node_review._latest_decision_row(current_rows)
        else:
            # No decision on the CURRENT hash — surface a PRIOR approved row so the
            # page can show "was approved by …" (edited_stale).
            prior_approved = lib_rows[lib_rows["decision"] == node_review.DECISION_APPROVE]
            if not prior_approved.empty:
                matched = node_review._latest_decision_row(prior_approved)
    return {
        "state": node_review.data_model_state(project_dir, schemas)["state"],
        "current_hash": current_hash,
        "matched_decision": matched,
    }


def _schema_spec(schema: dict[str, Any]) -> dict[str, Any]:
    """One schema with loader bookkeeping (_filename/_order/_error) removed — the
    spec only. The schema model is `extra="forbid"`, so validation and the edit
    textarea must both see the spec, never the bookkeeping keys the loader injects."""
    return {k: v for k, v in schema.items() if k not in node_review.HASH_IGNORED_KEYS}


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
    """DESTRUCTIVE — remove an entire project (schemas, workflow, runs, decisions,
    versions). Guarded via _project_dir: only a DIRECT child directory of examples/
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
        {"default_name": "", "default_doc": ""},
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
# shell reads ONLY from `state`; the workflow lock is the single source of truth
# state.data_model.state == "approved" (the SAME gate the SSE stream uses).


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
    """DOCUMENT — the source methodology document, read-only. The route reads the file
    server-side (the template never touches the filesystem); `document` is '' / None
    when the project has no document, and the template shows an empty state. The path
    line is state.document_path (absolute, truthful)."""
    pdir = _project_dir(project_name)
    state = shell_state(pdir)
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
    """DATA MODEL — named-schema cards by kind + ER diagram + the approval GATE + the
    per-schema edit + the authoring chat. Reuses the gated-flow render machinery
    (_schema_library_approval / _schema_json_map). The chat/approve/edit actions POST
    to the /project/{name}/data-model/... routes below."""
    pdir = _project_dir(project_name)
    schemas = load_schemas(pdir)
    approval = _schema_library_approval(pdir, schemas) if schemas else None
    return templates.TemplateResponse(
        request,
        "section_data_model.html",
        {
            "state": shell_state(pdir),
            "section": "data_model",
            "schemas": schemas,
            "er_diagram": build_schema_er_diagram(schemas) if schemas else None,
            "table_graph": build_schema_table_graph(schemas) if schemas else None,
            "issues": validate_schema_library([_schema_spec(s) for s in schemas]) if schemas else [],
            "approval": approval,
            "schema_json": _schema_json_map(schemas),
            "kind_order": SCHEMA_KIND_ORDER,
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
        },
    )


@router.get("/project/{project_name}/workflow", response_class=HTMLResponse)
async def project_workflow(request: Request, project_name: str):
    """WORKFLOW — the typed-stage pipeline: the mermaid graph coloured by belief, the
    per-node review split-view, and the Build / Run / Create-version controls. The
    version list lives on its own /workflow/versions tab, linked from here. Always
    navigable (the data-model→workflow nav lock is disabled pending a rethink);
    renders an empty state when no workflow is authored yet.

    Belief colouring uses the SAME spec dict (stage_to_spec_dict) the node-review
    decide route and the /review/status poller use, so the FIRST paint agrees with the
    live recolour. When the workflow validates we colour off typed Stages; a workflow
    with a broken stage still renders (as a draft graph off raw dicts) so the reviewer
    sees the holes — those draft nodes read as unreviewed (an invalid node can't carry
    a meaningful approval). Empty (not a 404) when there is no workflow yet, so the
    locked/empty page renders."""
    pdir = _project_dir(project_name)
    listing = load_stages_or_empty(project_name)
    decisions = node_review.load_node_decisions(pdir)
    coverage: dict[str, Any] | None
    stages: list[Any]
    if listing.stages:
        # Valid workflow: colour by the typed-stage spec dict (matches node_review
        # + /review/status), so an approved node paints green on first load.
        specs = [stage_to_spec_dict(s) for s in listing.stages]
        review_by_id = {
            s.id: node_review.approval_state_for(spec, decisions)["state"]
            for s, spec in zip(listing.stages, specs)
        }
        coverage = node_review.coverage_for(specs, decisions)
        stages = list(listing.stages)
    else:
        # No valid workflow. Fall back to raw draft dicts so a broken/partial workflow
        # still renders its graph with the holes visible; drafts read as unreviewed.
        draft = project._load_compiled_stages(pdir)
        review_by_id = {
            s["id"]: node_review.approval_state_for(s, decisions)["state"]
            for s in draft if s.get("id")
        }
        coverage = node_review.coverage_for(draft, decisions) if draft else None
        stages = draft
    mermaid = (
        build_mermaid_graph(stages, project_name, review_by_id=review_by_id)
        if stages else None
    )
    return templates.TemplateResponse(
        request,
        "section_workflow.html",
        {
            "state": shell_state(pdir),
            "section": "workflow",
            "stages": stages,
            "mermaid": mermaid,
            "coverage": coverage,
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


@router.post("/project/{project_name}/data-model/approve")
async def approve_data_model(project_name: str, content_hash: str = Form(...)):
    """The DATA-MODEL GATE. Record human approval of the whole schema library, keyed to
    the content_hash the page computed from the live schemas (so editing any schema
    later changes the hash and drops the approval to edited_stale, re-locking Phase 2).
    Guard: the POSTed hash must match the CURRENT library hash, else the approval would
    pin to a stale set — refuse loudly rather than approve the wrong thing."""
    pdir = _project_dir(project_name)

    schemas = load_schemas(pdir)
    if not schemas:
        raise HTTPException(
            status_code=400,
            detail="No data model to approve — author schemas in Phase 1 first.",
        )
    current_hash = node_review.schema_library_content_hash(schemas)
    if content_hash != current_hash:
        raise HTTPException(
            status_code=409,
            detail=(
                "Stale approval: the data model changed since the page loaded "
                f"(posted {content_hash[:12]}…, current {current_hash[:12]}…). "
                "Reload and approve the current data model."
            ),
        )
    node_review.approve_schema_library(pdir, content_hash=current_hash, reviewer="local")
    state = node_review.data_model_state(pdir, schemas)["state"]
    return JSONResponse({"ok": True, "state": state, "content_hash": current_hash})


@router.post("/project/{project_name}/schema/{schema_name}/edit")
async def edit_schema(project_name: str, schema_name: str, json_text: str = Form(...)):
    """The ONLY writer into examples/<name>/schemas/. Parse the posted JSON, validate
    it with validate_named_schema, and — only if clean — write it back to the schema's
    file. On validation issues return 400 with the issue list and write NOTHING (fail
    loudly, never a silent partial write). Editing changes the library hash, so a prior
    data-model approval auto-drops to edited_stale (re-locking Phase 2)."""
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
    schema = {k: v for k, v in parsed.items() if k not in node_review.HASH_IGNORED_KEYS}

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

    schemas = load_schemas(pdir)
    new_hash = node_review.schema_library_content_hash(schemas)
    state = node_review.data_model_state(pdir, schemas)["state"]
    return JSONResponse({"ok": True, "content_hash": new_hash, "state": state})
