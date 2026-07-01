"""
Methodology DAG visualization app (v2).

Reads compiled stage YAML files for each methodology, renders an interactive
DAG view plus per-stage detail pages that display the executable handle
(connector spec, prompt template, pandas function, join keys, aggregation
rules, queue config, or publish target) along with typed input/output schemas
and any eval/review configuration.

Run:
    python -m uvicorn app.main:app --reload --port 8765
Then open http://localhost:8765/
"""

from __future__ import annotations

import json
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.runtime.runner import execute_run, prepare_run, resume_run, run_prepared
from app.runtime.preview import run_stage_preview, PreviewError, PREVIEWABLE_TYPES

from app import node_review  # node-level APPROVAL / BELIEF state (Piece B)
from app import versioning  # immutable DAG version snapshots (Piece C)
from app.models import validate_stage  # single-stage contract (node-edit writer)

# Shared web primitives (templates, paths, DAG rendering, background runner) live
# in app.web_context so the compiler's route modules (app.pages / app.api.compile)
# can use them without importing this shell. Those modules own the /compile routes,
# mounted via include_router below.
from app.web_context import (
    EXAMPLES_DIR,
    REPO_ROOT,
    STATIC_DIR,
    SCHEMA_KIND_CLASS,
    SCHEMA_KIND_GLYPH,
    SCHEMA_KIND_ORDER,
    TYPE_CLASS,
    TYPE_GLYPH,
    build_mermaid_graph,
    get_input_ids,
    templates,
    _build_schema_er_diagram,
    _load_schemas,
    _run_in_background,
)
from app.models import validate_schema_library
from app import pages
from app import project  # PROJECT model — project_state snapshot for the unified sections
from app.api import compile as compile_api


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="Methodology DAG")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# The compiler feature's routes (pages + actions), split out of this shell.
app.include_router(pages.router)
app.include_router(compile_api.router)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def list_methodologies() -> list[dict[str, Any]]:
    """One project card per methodology dir under examples/ that has EITHER a DAG
    (compiled/*.yaml) OR a data model (schemas/*.yaml). Returns the shape the index
    dashboard renders: name, what's authored (has_dag / has_schemas), and the
    counts shown on the badge (stages, schema docs, runs). Sorted by name."""
    if not EXAMPLES_DIR.exists():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(EXAMPLES_DIR.iterdir()):
        if not p.is_dir():
            continue
        compiled_dir = p / "compiled"
        schemas_dir = p / "schemas"
        n_stages = len(list(compiled_dir.glob("*.yaml"))) if compiled_dir.is_dir() else 0
        has_dag = n_stages > 0
        has_schemas = schemas_dir.is_dir() and any(schemas_dir.glob("*.yaml"))
        # A schemas/*.yaml may hold multiple docs — count the loaded docs, not files.
        n_schemas = len(_load_schemas(p)) if has_schemas else 0
        runs_dir = p / "runs"
        n_runs = (
            sum(1 for r in runs_dir.iterdir() if r.is_dir()) if runs_dir.is_dir() else 0
        )
        if not (has_dag or has_schemas):
            continue
        out.append({
            "name": p.name,
            "has_dag": has_dag,
            "has_schemas": has_schemas,
            "n_stages": n_stages,
            "n_schemas": n_schemas,
            "n_runs": n_runs,
        })
    return out


def load_stages(methodology: str) -> list[dict[str, Any]]:
    compiled_dir = EXAMPLES_DIR / methodology / "compiled"
    if not compiled_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No compiled stages for {methodology}")
    stages: list[dict[str, Any]] = []
    for yaml_file in sorted(compiled_dir.glob("*.yaml")):
        with yaml_file.open("r", encoding="utf-8") as f:
            try:
                data = yaml.safe_load(f) or {}
            except yaml.YAMLError as exc:
                data = {
                    "id": yaml_file.stem,
                    "name": f"[YAML ERROR] {yaml_file.name}",
                    "type": "python_transform",
                    "compiler_notes": [f"YAML parse error: {exc}"],
                    "_error": True,
                }
        data["_filename"] = yaml_file.name
        data["_order"] = yaml_file.stem.split("_", 1)[0]
        stages.append(data)
    return stages


def build_er_diagram(stages: list[dict[str, Any]]) -> str:
    """Mermaid erDiagram showing each stage's output_schema as an entity, with
    PK markers, FK markers (inferred from upstream PKs), and edges from upstream
    to downstream stages."""
    lines = ["erDiagram"]

    # Index PK columns per stage so we can flag FKs.
    pk_owner: dict[str, str] = {}
    for s in stages:
        schema = s.get("output_schema") or {}
        for col in schema.get("primary_key") or []:
            pk_owner.setdefault(col, s["id"])

    # Entity definitions
    for s in stages:
        schema = s.get("output_schema") or {}
        cols = schema.get("columns") or []
        if not cols:
            continue
        pk_set = set(schema.get("primary_key") or [])
        lines.append(f"    {s['id']} {{")
        for col in cols:
            name = col.get("name", "")
            if not name:
                continue
            t = _safe_mermaid_type(col.get("type", "str"))
            marker = ""
            if name in pk_set:
                marker = "PK"
            elif name in pk_owner and pk_owner[name] != s["id"]:
                marker = "FK"
            label = col.get("description") or ""
            comment = ""
            if label:
                # mermaid erDiagram comment must be in quotes; cap length
                short = label.replace('"', "'")[:48]
                comment = f' "{short}"'
            line = f"        {t} {name}"
            if marker:
                line += f" {marker}"
            line += comment
            lines.append(line)
        lines.append("    }")

    # Relationship edges — one line per (upstream → downstream)
    for s in stages:
        for inp in s.get("inputs") or []:
            iid = inp.get("id") if isinstance(inp, dict) else inp
            if not iid:
                continue
            lines.append(f"    {iid} ||--o{{ {s['id']} : feeds")

    return "\n".join(lines)


def _safe_mermaid_type(t: str) -> str:
    """Mermaid erDiagram is picky — strip brackets, slashes, etc."""
    return (
        t.replace("[", "_")
         .replace("]", "")
         .replace(" ", "_")
         .replace(":", "_")
         .replace("+", "p")
    ) or "any"


def read_prose_excerpt(stage: dict[str, Any], methodology: str) -> str | None:
    src = stage.get("source") or {}
    doc = src.get("doc")
    if not doc:
        return None
    candidate = REPO_ROOT / doc
    if not candidate.exists():
        candidate = EXAMPLES_DIR / methodology / "stages" / Path(doc).name
        if not candidate.exists():
            return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


def _build_llm_example(
    stage_def: dict[str, Any], input_previews: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Render the prompt_template with the first row of the first usable input.

    Returns {rendered, source_id} on success, {error} if no input or render
    fails, or None if the stage isn't an LLM stage.
    """
    llm = (stage_def or {}).get("llm") or {}
    template = llm.get("prompt_template")
    if not template:
        return None
    for ip in input_previews:
        preview = ip.get("preview") or {}
        rows = preview.get("preview") or []
        if not rows:
            continue
        try:
            rendered = template.format(**rows[0])
        except (KeyError, IndexError, ValueError) as exc:
            return {
                "source_id": ip["id"],
                "error": f"could not render template: {type(exc).__name__}: {exc}",
            }
        return {"source_id": ip["id"], "rendered": rendered}
    return {"error": "no input rows available in this run to render an example"}


def read_module_code(module_path: str) -> str | None:
    """Resolve module 'examples.lobbymap.code.foo' to a file path and read it."""
    if not module_path:
        return None
    parts = module_path.split(".")
    candidate = REPO_ROOT / Path(*parts).with_suffix(".py")
    if not candidate.exists():
        return None
    try:
        return candidate.read_text(encoding="utf-8")
    except OSError:
        return None


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {"methodologies": list_methodologies()},
    )


@app.get("/methodology/{methodology}/schemas", response_class=HTMLResponse)
async def methodology_schemas(request: Request, methodology: str):
    """The DATA MODEL view — the NAMED schemas authored in examples/<name>/schemas/
    (kind palette + ER diagram), the "step 1" of a methodology. Distinct from
    /data-model, which derives an ER diagram from the DAG's output_schemas. 404 if
    this methodology has no data model. Reuses the same loader + ER builder the gated
    compile flow uses (now shared in web_context)."""
    methodology_dir = EXAMPLES_DIR / methodology
    schemas = _load_schemas(methodology_dir)
    if not schemas:
        raise HTTPException(
            status_code=404, detail=f"No data model (schemas/) for {methodology}"
        )
    has_dag = (methodology_dir / "compiled").is_dir() and any(
        (methodology_dir / "compiled").glob("*.yaml")
    )
    return templates.TemplateResponse(
        request,
        "schema_library.html",
        {
            "methodology": methodology,
            "schemas": schemas,
            "er_diagram": _build_schema_er_diagram(schemas),
            "issues": validate_schema_library(schemas),
            "has_dag": has_dag,
            "kind_order": SCHEMA_KIND_ORDER,
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
        },
    )


@app.post("/methodology/{methodology}/delete")
async def delete_methodology(methodology: str):
    """DESTRUCTIVE — remove an entire methodology project (schemas, DAG, runs,
    decisions, versions). Guarded carefully: resolve the target and refuse unless it
    is a DIRECT child directory of examples/ (so a traversal like '..%2f..', an
    absolute path, or a name resolving outside examples/ can never delete anything
    here). Only reachable via POST (the dashboard's confirm()-gated form) — there is
    deliberately no GET deletion. Redirects to the dashboard (303) on success."""
    target = (EXAMPLES_DIR / methodology).resolve()
    examples_root = EXAMPLES_DIR.resolve()
    if target.parent != examples_root or not target.is_dir():
        raise HTTPException(
            status_code=404, detail=f"No methodology '{methodology}' to delete"
        )
    shutil.rmtree(target)
    return RedirectResponse("/", status_code=303)


# ─── Unified PROJECT sections ────────────────────────────────────────────────
# One project (examples/<name>/) is framed by a left-sidebar shell (project_shell)
# with five sections — Overview / Document / Data model / Workflow / Runs. Each
# section route passes the SAME status snapshot (project.project_state) plus its
# section name and the section-specific extras the matching section_*.html needs.
# The shell reads ONLY from `state`; the workflow lock is the single source of
# truth state.data_model.state == "approved" (the SAME gate the SSE stream uses).


def _project_dir(methodology: str) -> Path:
    """Resolve a project dir and 404 if it isn't a real methodology working copy.
    Guards every section route: refuse anything that isn't a direct child of
    examples/ (no traversal/absolute path), mirroring delete_methodology's guard."""
    target = (EXAMPLES_DIR / methodology).resolve()
    if target.parent != EXAMPLES_DIR.resolve() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    return EXAMPLES_DIR / methodology


@app.get("/methodology/{methodology}", response_class=HTMLResponse)
async def project_overview(request: Request, methodology: str):
    """OVERVIEW — the project hub: identity (meta), the 5 status tiles, and the
    prominent "do this next" CTA. Renders ONLY from project_state (no section extras);
    unknown facts (a legacy model/date) show truthfully as 'unknown', never fabricated."""
    mdir = _project_dir(methodology)
    return templates.TemplateResponse(
        request,
        "section_overview.html",
        {"state": project.project_state(mdir), "section": "overview"},
    )


@app.get("/methodology/{methodology}/document", response_class=HTMLResponse)
async def project_document(request: Request, methodology: str):
    """DOCUMENT — the source methodology document, read-only. The route reads the
    file server-side (the template never touches the filesystem); `document` is '' /
    None when the project has no document, and the template shows an empty state. The
    path line is state.document_path (absolute, truthful)."""
    mdir = _project_dir(methodology)
    state = project.project_state(mdir)
    document = ""
    if state["document_path"]:
        try:
            document = Path(state["document_path"]).read_text(encoding="utf-8")
        except OSError:
            # The path came from project_state probing the disk; if it vanished
            # between snapshot and read, show the empty state rather than 500.
            document = ""
    return templates.TemplateResponse(
        request,
        "section_document.html",
        {"state": state, "section": "document", "document": document},
    )


@app.get("/methodology/{methodology}/data_model", response_class=HTMLResponse)
async def project_data_model(request: Request, methodology: str):
    """DATA MODEL — named-schema cards by kind + ER diagram + the approval GATE + the
    per-schema edit + the authoring chat. Reuses the gated-flow render machinery
    (_schema_library_approval / _schema_yaml_map live in app.api.compile), rehomed
    onto this project section. The chat/approve/edit actions POST to the rehomed
    /methodology/{name}/data-model/... routes."""
    mdir = _project_dir(methodology)
    schemas = _load_schemas(mdir)
    approval = compile_api._schema_library_approval(mdir, schemas) if schemas else None
    return templates.TemplateResponse(
        request,
        "section_data_model.html",
        {
            "state": project.project_state(mdir),
            "section": "data_model",
            "schemas": schemas,
            "er_diagram": _build_schema_er_diagram(schemas) if schemas else None,
            "issues": validate_schema_library(schemas) if schemas else [],
            "approval": approval,
            "schema_yaml": compile_api._schema_yaml_map(schemas),
            "kind_order": SCHEMA_KIND_ORDER,
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
        },
    )


@app.get("/methodology/{methodology}/workflow", response_class=HTMLResponse)
async def project_workflow(request: Request, methodology: str):
    """WORKFLOW — the typed-stage pipeline (formerly the DAG view): the mermaid graph
    coloured by belief, the per-node review split-view, the versions list, and the
    Build / Run / Cut-version controls. LOCKED in the template until the data model is
    approved — the SAME gate the SSE workflow stream enforces.

    Stages load via project._load_compiled_stages (the loader convention that injects
    _order/_filename), and is [] (not a 404) when there is no workflow yet, so the
    locked/empty page renders. Belief colours the FIRST paint (review_by_id), coverage
    drives the badge."""
    mdir = _project_dir(methodology)
    stages = project._load_compiled_stages(mdir)
    decisions = node_review.load_node_decisions(mdir)
    review_by_id = {
        s["id"]: node_review.approval_state_for(s, decisions)["state"]
        for s in stages if s.get("id")
    }
    coverage = node_review.coverage_for(stages, decisions) if stages else None
    mermaid = (
        build_mermaid_graph(stages, methodology, review_by_id=review_by_id)
        if stages else None
    )
    return templates.TemplateResponse(
        request,
        "section_workflow.html",
        {
            "state": project.project_state(mdir),
            "section": "workflow",
            "stages": stages,
            "mermaid": mermaid,
            "coverage": coverage,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "versions": versioning.list_versions(mdir),
        },
    )


@app.get("/methodology/{methodology}/stage/{stage_id}", response_class=HTMLResponse)
async def stage_view(request: Request, methodology: str, stage_id: str):
    stages = load_stages(methodology)
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    prose = read_prose_excerpt(stage, methodology)

    # If python_transform with module ref, try to read the source file
    function_code = None
    fn = stage.get("function") or {}
    if fn.get("kind") == "module" and fn.get("module"):
        function_code = read_module_code(fn["module"])
    elif fn.get("kind") == "inline":
        function_code = fn.get("code")

    return templates.TemplateResponse(
        request,
        "stage.html",
        {
            "methodology": methodology,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_yaml": yaml.safe_dump(stage, sort_keys=False, allow_unicode=True),
        },
    )


@app.get("/methodology/{methodology}/data-model", response_class=HTMLResponse)
async def data_model_view(request: Request, methodology: str):
    stages = load_stages(methodology)
    er = build_er_diagram(stages)
    return templates.TemplateResponse(
        request,
        "data_model.html",
        {
            "methodology": methodology,
            "stages": stages,
            "er_diagram": er,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@app.get("/methodology/{methodology}/stage/{stage_id}/partial", response_class=HTMLResponse)
async def stage_view_partial(request: Request, methodology: str, stage_id: str):
    """Stage detail content only — no <html> wrapper. Used by the split-view JS swap."""
    stages = load_stages(methodology)
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    prose = read_prose_excerpt(stage, methodology)
    function_code = None
    fn = stage.get("function") or {}
    if fn.get("kind") == "module" and fn.get("module"):
        function_code = read_module_code(fn["module"])
    elif fn.get("kind") == "inline":
        function_code = fn.get("code")
    return templates.TemplateResponse(
        request,
        "_stage_content.html",
        {
            "methodology": methodology,
            "stage": stage,
            "prose_excerpt": prose,
            "function_code": function_code,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "raw_yaml": yaml.safe_dump(stage, sort_keys=False, allow_unicode=True),
        },
    )


@app.get("/methodology/{methodology}/raw/{stage_id}", response_class=PlainTextResponse)
async def stage_raw_yaml(methodology: str, stage_id: str):
    stages = load_stages(methodology)
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    return yaml.safe_dump(stage, sort_keys=False, allow_unicode=True)


# ─── Node review (Piece B) ───────────────────────────────────────────────────
# NODE review = "do we trust HOW this step is modeled?" — colours the DAG by a
# content-hash approval state, does NOT halt a run. (Distinct from the ROW review
# queue below, which is "is this run's DATA right?" and DOES halt a run.) Mirrors
# the queue's decide/partial patterns, lifted from data rows to DAG node specs.


@app.get("/methodology/{methodology}/review/status")
async def review_status(methodology: str):
    """Live poller for the methodology page: belief state per node, coverage, and a
    freshly-built mermaid graph coloured by approval. Mirrors run_status — the page
    swaps `mermaid` in place after a decision/edit so the DAG recolours without a
    full reload."""
    stages = load_stages(methodology)
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    review_by_id = {
        s["id"]: node_review.approval_state_for(s, decisions)["state"]
        for s in stages if s.get("id")
    }
    coverage = node_review.coverage_for(stages, decisions)
    mermaid = build_mermaid_graph(stages, methodology, review_by_id=review_by_id)
    return JSONResponse({
        "review_by_id": review_by_id,
        "coverage": coverage,
        "mermaid": mermaid,
    })


@app.get("/methodology/{methodology}/node/{stage_id}/review-partial", response_class=HTMLResponse)
async def node_review_partial(request: Request, methodology: str, stage_id: str):
    """Per-node REVIEW/EDIT panel (right side of the methodology split view). Mirrors
    stage_view_partial, but answers the node-review question (approve / reject / edit
    the spec) instead of showing the read-only stage detail."""
    stages = load_stages(methodology)
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    decisions = node_review.load_node_decisions(EXAMPLES_DIR / methodology)
    review = node_review.approval_state_for(stage, decisions)
    return templates.TemplateResponse(
        request,
        "_node_review.html",
        {
            "methodology": methodology,
            "stage": stage,
            "review": review,
            "raw_yaml": yaml.safe_dump(stage, sort_keys=False, allow_unicode=True),
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )


@app.post("/methodology/{methodology}/node/{stage_id}/decide")
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
    stages = load_stages(methodology)
    stage = next((s for s in stages if s.get("id") == stage_id), None)
    if stage is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in {methodology}")
    decisions = node_review.load_node_decisions(methodology_dir)
    state = node_review.approval_state_for(stage, decisions)["state"]
    return JSONResponse({"ok": True, "state": state})


@app.post("/methodology/{methodology}/node/{stage_id}/edit")
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

    new_hash = node_review.node_content_hash(stage)
    decisions = node_review.load_node_decisions(methodology_dir)
    state = node_review.approval_state_for(stage, decisions)["state"]
    return JSONResponse({"ok": True, "content_hash": new_hash, "state": state})


# ─── Versioning (Piece C) ────────────────────────────────────────────────────


@app.post("/methodology/{methodology}/version")
async def cut_version_route(methodology: str, message: str = Form(...)):
    """Snapshot the working copy's {compiled/, schemas/} into a new immutable
    version + freeze approval coverage at cut time. The parent is the latest
    existing version (None for the very first cut). The JS redirects to the
    versions list on success."""
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    existing = versioning.list_versions(methodology_dir)  # newest-first
    parent = existing[0]["id"] if existing else None
    try:
        meta = versioning.cut_version(
            methodology_dir, message=message, reviewer="local", parent_version=parent
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse({"ok": True, "version": meta})


@app.get("/methodology/{methodology}/versions", response_class=HTMLResponse)
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


# ─── Run routes ─────────────────────────────────────────────────────────────

def _runs_dir(methodology: str) -> Path:
    return EXAMPLES_DIR / methodology / "runs"


def _list_runs(methodology: str) -> list[dict[str, Any]]:
    runs_dir = _runs_dir(methodology)
    if not runs_dir.is_dir():
        return []
    entries = []
    for run in sorted(runs_dir.iterdir(), reverse=True):
        if not run.is_dir():
            continue
        manifest_path = run / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                manifest = {"run_id": run.name, "status": "corrupt"}
            entries.append({
                "run_id": run.name,
                "status": manifest.get("status", "unknown"),
                "started_at": manifest.get("started_at"),
                "finished_at": manifest.get("finished_at"),
                # dag_version is None for legacy (pre-versioning) runs; the template
                # renders "(unversioned)" — a displayed truth, not a fabricated id.
                "dag_version": manifest.get("dag_version"),
                "stages_total": len(manifest.get("stages", [])),
                "stages_ok": sum(1 for s in manifest.get("stages", []) if s.get("status") == "ok"),
                "stages_error": sum(1 for s in manifest.get("stages", []) if s.get("status") == "error"),
            })
    return entries


@app.post("/methodology/{methodology}/run")
async def trigger_run(methodology: str):
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    # Pin the run to a DAG version: the latest existing one, or — if this
    # methodology has never been versioned — auto-cut an implicit version now so the
    # run records the REAL snapshot it executed against (never a blank/fabricated
    # id, never the mutable working copy). We resolve it here (rather than passing
    # version_id=None) so we can set the new version's parent and so the resolved id
    # is explicit at the call site. prepare_run loads stages from this snapshot.
    existing = versioning.list_versions(methodology_dir)  # newest-first
    if existing:
        version_id = existing[0]["id"]
    else:
        version_id = versioning.cut_version(
            methodology_dir, message="auto-cut on run", reviewer="system"
        )["id"]
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    prep = prepare_run(methodology_dir, REPO_ROOT, version_id)
    _run_in_background(run_prepared, prep)
    return RedirectResponse(
        url=f"/methodology/{methodology}/runs/{prep['run_id']}",
        status_code=303,
    )


@app.get("/methodology/{methodology}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, methodology: str):
    """RUNS — the runs list section (reuses _list_runs' shape), framed by the project
    shell, with awaiting-review runs surfaced (driven by state.runs.awaiting_review)."""
    mdir = _project_dir(methodology)
    return templates.TemplateResponse(
        request,
        "section_runs.html",
        {
            "state": project.project_state(mdir),
            "section": "runs",
            "runs": _list_runs(methodology),
        },
    )


@app.get("/methodology/{methodology}/runs/{run_id}/status")
async def run_status(methodology: str, run_id: str):
    """Lightweight JSON for the live poller: current status, per-stage statuses,
    counts, and a freshly-built mermaid graph. Lets the run page update progress
    in place (no full-page reload) so it stays clickable while running."""
    run_dir = _runs_dir(methodology) / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mstages = manifest.get("stages", [])
    status_by_id = {s["stage_id"]: s.get("status", "") for s in mstages}
    mermaid = build_mermaid_graph(load_stages(methodology), methodology, status_by_id=status_by_id)

    def _count(st: str) -> int:
        return sum(1 for s in mstages if s.get("status") == st)

    return JSONResponse({
        "status": manifest.get("status"),
        "terminal": manifest.get("status") != "running",
        "halted_at": manifest.get("halted_at"),
        "finished_at": manifest.get("finished_at"),
        "counts": {"ok": _count("ok"), "warn": _count("validation_warnings"),
                   "err": _count("error"), "total": len(mstages)},
        "stages": [{"stage_id": s["stage_id"], "status": s.get("status")} for s in mstages],
        "mermaid": mermaid,
    })


@app.get("/methodology/{methodology}/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, methodology: str, run_id: str):
    run_dir = _runs_dir(methodology) / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Load preview of each stage's output (first 5 rows)
    previews: dict[str, dict[str, Any]] = {}
    for s in manifest.get("stages", []):
        sid = s["stage_id"]
        op = s.get("output_path")
        if not op:
            continue
        path = run_dir / op
        if not path.exists():
            continue
        try:
            if path.suffix == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            previews[sid] = {"error": str(exc)}
            continue
        previews[sid] = {
            "columns": list(df.columns),
            "rows_total": len(df),
            "preview": df.head(5).fillna("").astype(str).to_dict(orient="records"),
        }

    # Build the run-page DAG from the VERSION'S frozen stages, not the working copy,
    # so the graph is an honest picture of what this run actually executed (working-
    # copy edits since the run must not change how a past run is drawn). Legacy runs
    # with no dag_version fall back to the working copy — the only place we read it,
    # and a truthful one (the template also labels them "(unversioned)").
    dag_version = manifest.get("dag_version")
    if dag_version:
        try:
            stages = versioning.load_version_stages(EXAMPLES_DIR / methodology, dag_version)
        except FileNotFoundError:
            # The snapshot dir is gone (e.g. deleted). Don't fabricate a graph from
            # the working copy as if it were the version; fail loudly so the gap is
            # visible rather than silently misrepresenting what ran.
            raise HTTPException(
                status_code=404,
                detail=f"Run {run_id} pinned to version '{dag_version}', "
                       f"but its snapshot is missing.",
            )
        # load_version_stages deliberately does NOT inject the loader bookkeeping
        # keys (_filename/_order) that load_stages adds, but build_mermaid_graph's
        # id-fallback references s["_filename"] — and dict.get evaluates that default
        # eagerly, so it KeyErrors on EVERY versioned stage without the key. Inject
        # it (derived from the id, never displayed — purely the mermaid id-fallback)
        # so the run-page DAG renders from the snapshot. Every stage has an id.
        for s in stages:
            s.setdefault("_filename", f"{s.get('id', 'stage')}.yaml")
    else:
        stages = load_stages(methodology)
    status_by_id = {s["stage_id"]: s.get("status", "") for s in manifest.get("stages", [])}
    mermaid = build_mermaid_graph(stages, methodology, status_by_id=status_by_id)

    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {
            "methodology": methodology,
            "run_id": run_id,
            "manifest": manifest,
            "previews": previews,
            "mermaid": mermaid,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@app.get(
    "/methodology/{methodology}/runs/{run_id}/stage/{stage_id}/partial",
    response_class=HTMLResponse,
)
async def run_stage_partial(
    request: Request, methodology: str, run_id: str, stage_id: str
):
    """Per-run stage detail panel — status, validation, preview, error trace."""
    run_dir = _runs_dir(methodology) / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    stage_record = next(
        (s for s in manifest.get("stages", []) if s.get("stage_id") == stage_id),
        None,
    )
    if stage_record is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}' in run")

    def _load_preview(rel_path: str | None) -> dict[str, Any] | None:
        if not rel_path:
            return None
        path = run_dir / rel_path
        if not path.exists():
            return {"error": f"missing on disk: {rel_path}"}
        try:
            if path.suffix == ".parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path)
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc)}
        return {
            "columns": list(df.columns),
            "rows_total": len(df),
            "preview": df.head(5).fillna("").astype(str).to_dict(orient="records"),
        }

    output_preview = _load_preview(stage_record.get("output_path"))

    # Build input previews from upstream stages' outputs in this run.
    stages_static = load_stages(methodology)
    stage_def = next((s for s in stages_static if s.get("id") == stage_id), None)
    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }
    input_previews: list[dict[str, Any]] = []
    if stage_def is not None:
        for input_id in get_input_ids(stage_def):
            input_previews.append(
                {
                    "id": input_id,
                    "preview": _load_preview(output_by_id.get(input_id)),
                }
            )

    function_code = None
    fn = (stage_def or {}).get("function") or {}
    if fn.get("kind") == "module" and fn.get("module"):
        function_code = read_module_code(fn["module"])
    elif fn.get("kind") == "inline":
        function_code = fn.get("code")

    llm_example = _build_llm_example(stage_def, input_previews) if stage_def else None

    return templates.TemplateResponse(
        request,
        "_run_stage_panel.html",
        {
            "methodology": methodology,
            "run_id": run_id,
            "stage": stage_record,
            "stage_def": stage_def,
            "preview": output_preview,
            "input_previews": input_previews,
            "function_code": function_code,
            "llm_example": llm_example,
            "previewable": (stage_def or {}).get("type") in PREVIEWABLE_TYPES,
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


@app.post("/methodology/{methodology}/runs/{run_id}/stage/{stage_id}/preview")
async def run_stage_scratch_preview(
    request: Request, methodology: str, run_id: str, stage_id: str
):
    """SCRATCH in-memory re-run of one stage on a few selected input rows.

    Reads the chosen rows from this run's upstream outputs, runs the stage's
    handler in memory, and returns the output rows as JSON. Nothing is
    persisted: no manifest change, no output file, no artifact. Used by the
    node-detail panel's "Run transform on selected" button.

    Body (JSON): {"indices": [int, ...]}  — positional row indices into the
    stage's first upstream input.
    """
    run_dir = _runs_dir(methodology) / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    indices_raw = (body or {}).get("indices", [])
    indices: list[int] = []
    for i in indices_raw:
        try:
            indices.append(int(i))
        except (TypeError, ValueError):
            continue

    stages_static = load_stages(methodology)
    stage_def = next((s for s in stages_static if s.get("id") == stage_id), None)
    if stage_def is None:
        raise HTTPException(status_code=404, detail=f"No stage '{stage_id}'")

    output_by_id = {
        s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])
    }

    try:
        result = run_stage_preview(
            stage_def=stage_def,
            run_dir=run_dir,
            repo_root=REPO_ROOT,
            methodology_dir=EXAMPLES_DIR / methodology,
            output_by_id=output_by_id,
            selected_indices=indices,
        )
    except PreviewError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — surface the real failure
        return JSONResponse(
            {"ok": False, "error": f"{type(exc).__name__}: {exc}",
             "traceback": traceback.format_exc(limit=8)},
            status_code=500,
        )

    return JSONResponse({"ok": True, **result})


@app.get("/methodology/{methodology}/runs/{run_id}/artifact/{filename:path}", response_class=HTMLResponse)
async def run_artifact(methodology: str, run_id: str, filename: str):
    """Serve generated HTML artifacts (per-org profiles etc.) inline."""
    run_dir = _runs_dir(methodology) / run_id
    candidate = (run_dir / "artifacts" / filename).resolve()
    if not candidate.exists() or not str(candidate).startswith(str(run_dir.resolve())):
        raise HTTPException(status_code=404, detail="Artifact not found")
    return HTMLResponse(content=candidate.read_text(encoding="utf-8"))


# ─── Review queue ────────────────────────────────────────────────────────────


def _decisions_path(methodology: str, stage_id: str) -> Path:
    d = EXAMPLES_DIR / methodology / "decisions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stage_id}.parquet"


def _load_decisions_df(methodology: str, stage_id: str) -> pd.DataFrame:
    p = _decisions_path(methodology, stage_id)
    if not p.exists():
        return pd.DataFrame(
            columns=["content_hash", "decision", "modified_score",
                     "reviewer", "reviewed_at", "source_run_id"]
        )
    return pd.read_parquet(p)


def _display_cell(v: Any) -> Any:
    """Scalar-safe cell formatting for the reviewer UI. pd.isna() raises on
    list/array-valued cells (e.g. an evidence_urls JSON column), so handle
    array-likes explicitly before the null check."""
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) if len(v) else ""
    if hasattr(v, "tolist") and not isinstance(v, str):  # numpy array from parquet
        seq = v.tolist()
        return ", ".join(str(x) for x in seq) if len(seq) else ""
    try:
        return "" if pd.isna(v) else v
    except (ValueError, TypeError):
        return v


def _queue_snapshot(methodology: str, run_id: str, stage_id: str) -> pd.DataFrame | None:
    run_dir = _runs_dir(methodology) / run_id
    for ext in (".parquet", ".csv"):
        p = run_dir / "queue" / f"{stage_id}{ext}"
        if p.exists():
            return pd.read_parquet(p) if ext == ".parquet" else pd.read_csv(p)
    return None


@app.get("/methodology/{methodology}/runs/{run_id}/queue/{stage_id}", response_class=HTMLResponse)
async def queue_page(request: Request, methodology: str, run_id: str, stage_id: str):
    """Reviewer UI for one queue stage in one run."""
    run_dir = _runs_dir(methodology) / run_id
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Run not found")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    stages = load_stages(methodology)
    stage_def = next((s for s in stages if s.get("id") == stage_id), None)
    if stage_def is None or stage_def.get("type") != "human_review_queue":
        raise HTTPException(status_code=404, detail=f"No queue stage '{stage_id}'")

    snapshot = _queue_snapshot(methodology, run_id, stage_id)
    decisions = _load_decisions_df(methodology, stage_id)
    decision_by_hash: dict[str, dict[str, Any]] = {}
    if len(decisions):
        for _, row in decisions.iterrows():
            decision_by_hash[row["content_hash"]] = {
                "decision": row.get("decision"),
                "modified_score": row.get("modified_score"),
                "reviewer": row.get("reviewer"),
                "reviewed_at": row.get("reviewed_at"),
            }

    items: list[dict[str, Any]] = []
    if snapshot is not None:
        for _, row in snapshot.iterrows():
            h = row["content_hash"]
            existing = decision_by_hash.get(h)
            items.append({
                "content_hash": h,
                "row": {k: _display_cell(v) for k, v in row.items()
                        if k not in ("content_hash", "decision", "modified_score",
                                     "reviewer", "reviewed_at")},
                "prior_decision": existing,
            })

    reviewed_count = sum(1 for i in items if i["prior_decision"] is not None)
    total = len(items)

    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "methodology": methodology,
            "run_id": run_id,
            "stage_id": stage_id,
            "stage_def": stage_def,
            "items": items,
            "reviewed_count": reviewed_count,
            "total": total,
            "all_reviewed": total > 0 and reviewed_count == total,
            "manifest_status": manifest.get("status"),
        },
    )


@app.post("/methodology/{methodology}/runs/{run_id}/queue/{stage_id}/decide")
async def queue_decide(
    methodology: str,
    run_id: str,
    stage_id: str,
    content_hash: str = Form(...),
    decision: str = Form(...),
    modified_score: str | None = Form(None),
):
    """Persist a reviewer's decision against a content_hash."""
    if decision not in ("approve", "reject", "modify"):
        raise HTTPException(status_code=400, detail=f"unknown decision '{decision}'")
    mod_val: float | None = None
    if decision == "modify":
        if modified_score in (None, ""):
            raise HTTPException(status_code=400, detail="modify requires modified_score")
        try:
            mod_val = float(modified_score)
        except ValueError:
            raise HTTPException(status_code=400, detail="modified_score must be numeric")

    df = _load_decisions_df(methodology, stage_id)
    df = df[df["content_hash"] != content_hash]  # upsert: drop prior row if any
    new_row = {
        "content_hash": content_hash,
        "decision": decision,
        "modified_score": mod_val,
        "reviewer": "local",
        "reviewed_at": datetime.now().isoformat(timespec="seconds"),
        "source_run_id": run_id,
    }
    df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
    df.to_parquet(_decisions_path(methodology, stage_id), index=False)
    return JSONResponse({"ok": True, "content_hash": content_hash, "decision": decision})


@app.post("/methodology/{methodology}/runs/{run_id}/resume")
async def resume_run_route(methodology: str, run_id: str):
    """Resume a halted run from where it stopped. Used after all queue
    items have decisions."""
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    run_dir = _runs_dir(methodology) / run_id
    if not (run_dir / "manifest.json").exists():
        raise HTTPException(status_code=404, detail="Run not found")
    # Resume re-runs the queue stage + downstream (LLM-heavy) — do it in the
    # background and redirect immediately so the page can poll progress.
    _run_in_background(resume_run, methodology_dir, run_id, REPO_ROOT)
    return RedirectResponse(
        url=f"/methodology/{methodology}/runs/{run_id}",
        status_code=303,
    )
