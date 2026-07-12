"""
project.py (router) — the PROJECT SHELL + gated authoring.

A project is one directory under examples/<name>/ framed by a left-sidebar shell
(Overview · Document · Data model · Workflow · Runs). This router owns:

  Home dashboard
    GET  /                                       — project cards + create + delete
    POST /project/{project}/delete               — DESTRUCTIVE remove (guarded)

  Section pages (each renders project_shell.html via a section_*.html body)
    GET  /project/{project}                      — Overview
    GET  /project/{project}/document             — Document (read-only source)
    GET  /project/{project}/data_model           — Data model + ER + the approval GATE
    GET  /project/{project}/workflow             — Workflow (belief graph + node review)

  Create + data-model gate
    GET  /project/new                            — the paste-doc create form
    POST /project/new                            — create examples/<name>/ + project.json
    POST /project/{project}/data-model/approve   — record the schema-library approval (gate)
    POST /project/{project}/schema/{name}/edit   — the only writer into schemas/

The Runs section page (GET /project/{project}/runs) is served by app.web.routers.runs
so it stays next to the run lifecycle it renders. Per-stage detail is served inline by
the workflow section (node review at /project/{project}/node/{id}/review-partial, in
app.web.routers.node_review), so this router carries no standalone stage-detail view.

Route order matters: the literal /project/new is declared on THIS router BEFORE the
/project/{project} section routes, so "new" is matched as a literal, never captured as
a project name. The two-word section paths (/data_model, /workflow, /document) never
collide with a project name.

Reuse rule: reuses P1's node_review (belief + schema-library gate) + versioning, P2's
compiler (compile_methodology), and the shared web helpers (diagrams, loading,
config). The app.models package is the only contract.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
)

from app.models import validate_named_schema, validate_schema_library
from app.services import generation, node_review, project, versioning
from app.services.loader import stage_to_spec_dict
from app.web.config import EXAMPLES_DIR, templates
from app.web.diagrams import (
    SCHEMA_KIND_CLASS,
    SCHEMA_KIND_GLYPH,
    SCHEMA_KIND_ORDER,
    TYPE_CLASS,
    TYPE_GLYPH,
    build_mermaid_graph,
    build_schema_er_diagram,
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
    target = (EXAMPLES_DIR / project_name).resolve()
    if target.parent != EXAMPLES_DIR.resolve() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No project '{project_name}'")
    return EXAMPLES_DIR / project_name


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
    return {k: v for k, v in schema.items() if k not in node_review.CANONICAL_IGNORE_KEYS}


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
    authoring starts. The directory IS the session — the data-model / workflow streams
    key off the project name and read document.md / write chat.jsonl in here.

    Truthfulness: we write project.json (via project.write_project_meta) so a NEW
    project carries a real model + created_at (non-legacy); we never fabricate those
    for legacy dirs. A name clash fails LOUDLY (400) rather than clobbering existing
    data — the rename is the human's decision."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "project"
    doc = doc_text.strip()
    if not doc:
        raise HTTPException(status_code=400, detail="The methodology document is empty.")

    project_dir = EXAMPLES_DIR / safe_name
    # Don't silently overwrite an existing project's data — fail loudly so a name clash
    # is the human's decision, not a quiet clobber.
    if project_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"examples/{safe_name}/ already exists — choose a different name.",
        )

    project_dir.mkdir(parents=True, exist_ok=True)
    # The source of record travels WITH the project (document.md is the canonical name
    # project_state probes first). The data-model stream seeds Phase 1 from it.
    (project_dir / "document.md").write_text(doc, encoding="utf-8")
    # Record identity so the project is NON-legacy: a real model + creation time +
    # source (never a fabricated default — write_project_meta persists exactly these).
    project.write_project_meta(
        project_dir,
        name=safe_name,
        title=None,
        created_at=datetime.now().isoformat(timespec="seconds"),
        model=model,
        source="pasted document",
    )
    # Kick off automatic generation (data model → then workflow) in the background.
    # The pages read the result from disk (no live status yet — issue #95); nothing is
    # authored by hand here.
    generation.start_generation(project_dir, document=doc, model=model)
    # Land on the data-model section — that's where the generation spinner (and then
    # the generated schemas) render. The document section is read-only context.
    return RedirectResponse(url=f"/project/{safe_name}/data_model", status_code=303)


@router.post("/project/{project_name}/generate")
async def generate_project(project_name: str):
    """(Re)kick automatic data-model → workflow generation for an EXISTING project —
    the manual counterpart to the auto-kick on create (for a legacy project that has a
    document but no data model, or to regenerate from scratch). Reads document.md + the
    project's model and starts the background run, then redirects to the data-model
    page (which shows the spinner). 400 if there is no document to generate from."""
    pdir = _project_dir(project_name)
    document_path = pdir / "document.md"
    if not document_path.is_file():
        raise HTTPException(
            status_code=400,
            detail=f"examples/{project_name}/ has no document.md to generate from.",
        )
    model = project.project_meta(pdir).model or "sonnet"
    generation.start_generation(
        pdir, document=document_path.read_text(encoding="utf-8"), model=model
    )
    return RedirectResponse(url=f"/project/{project_name}/data_model", status_code=303)


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
    per-node review split-view, the versions list, and the Build / Run / Create-version
    controls. LOCKED in the template until the data model is approved — the SAME gate
    the SSE workflow stream enforces.

    Belief colouring uses the SAME canonical spec (stage_to_spec_dict) the node-review
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
        # Valid workflow: colour by the canonical typed-stage spec (matches node_review
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
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "versions": versioning.list_versions(pdir),
        },
    )


# ─── Data-model gate + schema edit (keyed on the PROJECT) ────────────────────
# Human review actions on an existing data model: approve the whole schema library
# (the gate that unlocks the workflow view) and edit a single schema — both key off
# the project dir under examples/<name>/.


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
    schema = {k: v for k, v in parsed.items() if k not in node_review.CANONICAL_IGNORE_KEYS}

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
