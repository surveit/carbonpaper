"""
api/compile.py — the COMPILER feature's ACTION / JSON routes.

Split out of `app/main.py` (alongside the page routes in `app/pages.py`):

    POST /compile/new                    — kick off a compile (background) + redirect
    GET  /compile/{compilation_id}/status — lightweight JSON for the live poller

GATED AUTHORING (PR#12, rehomed onto the PROJECT) — a human-gated, two-phase
authoring flow that builds a methodology working copy (examples/<name>/) directly:
the methodology DIRECTORY is the session (chat.jsonl + document.md live in
examples/<name>/), so authoring no longer needs a separate compilation id. These
routes are the actions behind the unified project sections (app.main owns the
section PAGES that render them):

    GET  /methodology/new                       — the paste-doc create form
    POST /methodology/new                        — create examples/<name>/ + project.json, redirect
    GET  /methodology/{m}/data-model/stream      — SSE: Phase 1 (schemas, STOPS); seeds from the doc
    POST /methodology/{m}/data-model/approve     — record the schema-library approval (the gate)
    POST /methodology/{m}/schema/{name}/edit     — the only writer into examples/<name>/schemas/
    GET  /methodology/{m}/workflow/stream        — SSE: Phase 2 (stages, after approval; 409 if not)

Back-compat: GET /compile/new-methodology → 302 /methodology/new. The old per-id
gated pages/streams (/compile/{id}/gated|data_model|dag …) are removed — the
project is the unit now and nothing links there. The legacy one-shot prose flow
(/compile/new + /compile/{id}) is untouched.

Declared on an APIRouter that `main.py` mounts via app.include_router(). These
project-keyed routes are registered (via the router) BEFORE main.py's catch-all
`/methodology/{methodology}`, so GET /methodology/new is matched as a literal and
not captured as a methodology name.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from app import compiler
from app import node_review  # schema-library APPROVAL (the data-model gate)
from app import project  # PROJECT model — write_project_meta for new working copies
from app.dag_schema import validate_named_schema, validate_schema_library
from app.web_context import (
    COMPILATIONS_DIR,
    EXAMPLES_DIR,
    REPO_ROOT,
    RESEARCH_RUNS_DIR,
    SCHEMA_KIND_CLASS,
    SCHEMA_KIND_GLYPH,
    SCHEMA_KIND_ORDER,
    _build_schema_er_diagram,
    _load_schemas,
    _run_in_background,
    templates,
)

router = APIRouter()


# ─── Gated-compile helpers (Piece D) ─────────────────────────────────────────
# The gated page renders the data model (named schemas) authored into the
# methodology working copy — examples/<name>/schemas/. The schema loader, ER-diagram
# builder, and kind palette (SCHEMA_KIND_*) now live in app.web_context so the
# methodology data-model route (app.main) can reuse them without an import cycle;
# the approval / edit helpers below stay here (they're specific to the gated flow).


def _schema_library_approval(
    methodology_dir: Path, schemas: list[dict[str, Any]]
) -> dict[str, Any]:
    """Build the gated page's `approval` dict for the WHOLE schema library:
    {state, current_hash, matched_decision}.

    current_hash is the schema_library_content_hash (POSTed back on approve and the
    exact value approve_schema_library records), NOT a hash recomputed from a synthetic
    node — so the displayed hash, the approve POST, and the stored decision agree.
    matched_decision is the latest row that determined the state, so the template can
    attribute "(was) approved by <reviewer> at <ts>" — pulled from the SAME
    SCHEMA_LIBRARY_STAGE_ID rows data_model_state reads."""
    current_hash = node_review.schema_library_content_hash(schemas)
    df = node_review.load_node_decisions(methodology_dir)
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
        "state": node_review.data_model_state(methodology_dir, schemas)["state"],
        "current_hash": current_hash,
        "matched_decision": matched,
    }


def _schema_yaml_map(schemas: list[dict[str, Any]]) -> dict[str, str]:
    """name → YAML text for the per-schema edit textareas. Bookkeeping keys
    (_filename/_error) injected by the loader are stripped so the editable text is
    the spec only (and round-trips through the schema-edit writer cleanly)."""
    out: dict[str, str] = {}
    for s in schemas:
        name = s.get("name")
        if not name:
            continue
        spec = {k: v for k, v in s.items() if k not in node_review.CANONICAL_IGNORE_KEYS}
        out[name] = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=100)
    return out


def _methodology_dir(name: str) -> Path:
    """Resolve a methodology working-copy dir for the project-keyed authoring routes
    and refuse anything that isn't a direct child of examples/ (no traversal, no
    absolute path). Raises 404 if the methodology doesn't exist — the authoring
    actions revise an EXISTING project (created via POST /methodology/new); they
    never create the dir as a side effect."""
    target = (EXAMPLES_DIR / name).resolve()
    if target.parent != EXAMPLES_DIR.resolve() or not target.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{name}'")
    return target


@router.post("/compile/new")
async def compile_new_submit(
    source: str = Form(...),
    out_name: str = Form(...),
    model: str = Form("sonnet"),
):
    """Run + PERSIST a compilation as a first-class object, then redirect to its
    detail page. The compile is a multi-minute LLM call, so — exactly like
    trigger_run — we write an initial `running` manifest, kick the work off in a
    background thread, and redirect immediately. The detail page polls."""
    input_path = (REPO_ROOT / source).resolve()
    # Confine to the research_runs dir (no arbitrary path read).
    if RESEARCH_RUNS_DIR.resolve() not in input_path.parents or not input_path.is_file():
        raise HTTPException(status_code=400, detail=f"Unknown input: {source}")

    safe_name = re.sub(r"[^a-z0-9_]", "_", out_name.strip().lower()) or "compiled"

    prep = compiler.prepare_compilation(
        COMPILATIONS_DIR, str(input_path), safe_name, model
    )
    _run_in_background(compiler.run_prepared_compilation, prep)
    return RedirectResponse(url=f"/compile/{prep['compilation_id']}", status_code=303)


@router.get("/compile/{compilation_id}/status")
async def compilation_status(compilation_id: str):
    """Lightweight JSON for the live poller while a compile runs (parallels
    run_status): just the manifest status + terminal flag."""
    manifest_path = COMPILATIONS_DIR / compilation_id / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(status_code=404, detail="Compilation not found")
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    return JSONResponse({
        "status": m.get("status"),
        "terminal": m.get("status") != "running",
        "n_stages": m.get("n_stages", 0),
        "n_validation_issues": len(m.get("validation_issues") or []),
    })


# ─── New methodology (paste doc → project) ───────────────────────────────────
# Create a methodology working copy directly under examples/<name>/. The DIRECTORY
# is the authoring session now (no separate compilation id): the pasted document
# lands at examples/<name>/document.md and the chat transcript at chat.jsonl, so
# the gated authoring streams below key off the methodology NAME, not a comp id.
# (The back-compat GET/POST /compile/new-methodology → /methodology/new redirects
# live in app.pages, where they register BEFORE pages' /compile/{compilation_id}
# catch-all — otherwise "new-methodology" would be matched as a compilation id.)


@router.get("/methodology/new", response_class=HTMLResponse)
async def new_methodology_form(request: Request):
    """The project CREATE form — paste a methodology doc that lands in a new
    examples/<name>/ working copy. Authoring then proceeds in the project's own
    sections (data model → approve → workflow). Declared on this router so it is
    registered BEFORE main.py's catch-all /methodology/{methodology} — otherwise
    'new' would be captured as a methodology name."""
    return templates.TemplateResponse(
        request,
        "compile_new_methodology.html",
        {"default_name": "", "default_doc": ""},
    )


@router.post("/methodology/new")
async def new_methodology_submit(
    name: str = Form(...),
    doc_text: str = Form(...),
    model: str = Form("sonnet"),
):
    """Create the examples/<name>/ working copy + its project.json, persist the pasted
    document at document.md, then redirect to the project's data-model section where
    authoring starts. The directory IS the session — the data-model / workflow streams
    key off the methodology name and read document.md / write chat.jsonl in here.

    Truthfulness: we write project.json (via project.write_project_meta) so a NEW
    project carries a real model + created_at (non-legacy); we never fabricate those
    for legacy dirs. A name clash fails LOUDLY (400) rather than clobbering existing
    data — the rename is the human's decision."""
    safe_name = re.sub(r"[^a-z0-9_]", "_", name.strip().lower()) or "methodology"
    doc = doc_text.strip()
    if not doc:
        raise HTTPException(status_code=400, detail="The methodology document is empty.")

    methodology_dir = EXAMPLES_DIR / safe_name
    # Don't silently overwrite an existing methodology's data — fail loudly so a name
    # clash is the human's decision, not a quiet clobber.
    if methodology_dir.exists():
        raise HTTPException(
            status_code=400,
            detail=f"examples/{safe_name}/ already exists — choose a different name.",
        )

    methodology_dir.mkdir(parents=True, exist_ok=True)
    # The source of record travels WITH the methodology (document.md is the canonical
    # name project_state probes first). The data-model stream seeds Phase 1 from it.
    (methodology_dir / "document.md").write_text(doc, encoding="utf-8")
    # Record identity so the project is NON-legacy: a real model + creation time +
    # source (never a fabricated default — write_project_meta persists exactly these).
    project.write_project_meta(
        methodology_dir,
        name=safe_name,
        title=None,
        created_at=datetime.now().isoformat(timespec="seconds"),
        model=model,
        source="pasted document",
    )
    # Land on the data-model section — authoring starts there (the document section is
    # read-only context).
    return RedirectResponse(url=f"/methodology/{safe_name}/data_model", status_code=303)


# ─── Gated authoring streams + gate (rehomed onto the PROJECT) ───────────────
# The two-phase, human-gated authoring actions, re-keyed from a compilation id to
# the methodology NAME. The methodology dir IS the session: per the compiler
# contract for a merged project, BOTH comp_dir and methodology_dir are the SAME
# examples/<name>/ dir (chat.jsonl lives there). The section PAGES that render
# these live in app.main; here are the actions they POST/stream to.


def _gated_sse(methodology_dir: Path, message: str, model: str, phase: str):
    """Wrap compiler.stream_compile_chat (an async generator) as Server-Sent Events.
    Each event dict becomes one `data: <json>` line the page's EventSource decodes.
    The generator yields its own terminal {data_model_proposed|done|error} event, so
    the stream ends cleanly without inventing a sentinel.

    For a merged project the comp_dir and methodology_dir are the SAME directory
    (examples/<name>/) — the session and the working copy are one, so chat.jsonl and
    the schemas/compiled output co-locate."""
    async def _gen():
        async for event in compiler.stream_compile_chat(
            methodology_dir,
            user_message=message,
            history=None,
            model=model,
            phase=phase,
            methodology_dir=methodology_dir,
        ):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    return _gen()


def _project_model(methodology_dir: Path) -> str:
    """The model to author with: the project's recorded model, else 'sonnet'. A
    legacy project has no project.json (model is None) — fall back to the quality
    default rather than fail, since authoring is interactive (the human can steer)."""
    return project.project_meta(methodology_dir).get("model") or "sonnet"


@router.get("/methodology/{methodology}/data-model/stream")
async def data_model_stream(methodology: str, message: str = ""):
    """SSE — Phase 1: stream the DATA MODEL (named schemas) into
    examples/<name>/schemas/ and STOP (the model must not author the workflow; stray
    stage blocks are dropped + surfaced by stream_compile_chat). The browser
    EventSource can only GET, so the journalist's message arrives as the `message`
    query param.

    Seed Phase 1 from the project's source document (document.md / methodology_raw.*).
    The browser opener sends an empty `message` ("read the document"); a typed message
    is steering. Either way the document is the source of record the model authors from
    — fed in HERE (stream_compile_chat is input-agnostic and never reads the document)."""
    methodology_dir = _methodology_dir(methodology)
    doc_path = project._document_path(methodology_dir)
    document = doc_path.read_text(encoding="utf-8").strip() if doc_path else ""
    seed = message.strip()
    if document and seed:
        user_message = f"# Methodology document\n{document}\n\n# Instruction\n{seed}"
    elif document:
        user_message = (
            "Author the DATA MODEL — the named schemas (the tables this methodology "
            "operates on) — for the methodology described in the document below. "
            "Describe each table briefly, then emit it. Do NOT design the workflow yet.\n\n"
            f"# Methodology document\n{document}"
        )
    else:
        # No document on disk (creation requires a non-empty doc, so this is a defect
        # path) — use whatever was typed rather than fabricate input.
        user_message = seed
    return StreamingResponse(
        _gated_sse(methodology_dir, user_message, _project_model(methodology_dir), "data_model"),
        media_type="text/event-stream",
    )


@router.get("/methodology/{methodology}/workflow/stream")
async def workflow_stream(methodology: str, message: str = ""):
    """SSE — Phase 2: stream the WORKFLOW STAGES wiring the APPROVED data model into
    examples/<name>/compiled/. The DATA-MODEL GATE is enforced HERE, at the HTTP
    layer: Phase 2 streaming is refused with 409 unless the live schema library is
    in the `approved` state. (stream_compile_chat(phase='dag') additionally fails
    loudly if no schemas exist at all, but that only catches an EMPTY data model —
    schemas authored in Phase 1 but not yet approved would otherwise slip through,
    so the approval check is the actual gate.) Editing a schema after approval drops
    the state to `edited_stale`, which re-locks this route until re-approval.

    (The internal compiler phase string stays "dag"; only the URL/UI say workflow.)"""
    methodology_dir = _methodology_dir(methodology)

    schemas = _load_schemas(methodology_dir)
    state = node_review.data_model_state(methodology_dir, schemas)["state"]
    if state != "approved":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Data-model gate: cannot build the workflow while the data model is "
                f"'{state}'. Approve the data model first before streaming the "
                f"workflow."
            ),
        )

    return StreamingResponse(
        _gated_sse(methodology_dir, message, _project_model(methodology_dir), "dag"),
        media_type="text/event-stream",
    )


@router.post("/methodology/{methodology}/data-model/approve")
async def approve_data_model(methodology: str, content_hash: str = Form(...)):
    """The DATA-MODEL GATE. Record human approval of the whole schema library, keyed
    to the content_hash the page computed from the live schemas (so editing any schema
    later changes the hash and drops the approval to edited_stale, re-locking Phase 2).
    Guard: the POSTed hash must match the CURRENT library hash, else the approval would
    pin to a stale set — refuse loudly rather than approve the wrong thing."""
    methodology_dir = _methodology_dir(methodology)

    schemas = _load_schemas(methodology_dir)
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
    node_review.approve_schema_library(methodology_dir, content_hash=current_hash, reviewer="local")
    state = node_review.data_model_state(methodology_dir, schemas)["state"]
    return JSONResponse({"ok": True, "state": state, "content_hash": current_hash})


@router.post("/methodology/{methodology}/schema/{schema_name}/edit")
async def edit_schema(methodology: str, schema_name: str, yaml_text: str = Form(...)):
    """The ONLY writer into examples/<name>/schemas/. Parse the posted YAML, validate
    it with validate_named_schema, and — only if clean — write it back to the schema's
    file. On validation issues return 400 with the issue list and write NOTHING (fail
    loudly, never a silent partial write). Editing changes the library hash, so a prior
    data-model approval auto-drops to edited_stale (re-locking Phase 2)."""
    methodology_dir = _methodology_dir(methodology)

    # Parse — a parse error is the reviewer's, surfaced as a 400 issue, file untouched.
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return JSONResponse({"ok": False, "issues": [f"YAML parse error: {exc}"]}, status_code=400)
    if not isinstance(parsed, dict):
        return JSONResponse(
            {"ok": False, "issues": ["edited schema must be a YAML mapping (a single schema dict)"]},
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
             "issues": [f"name in the edited YAML ('{parsed_name}') must equal the schema name '{schema_name}'"]},
            status_code=400,
        )

    issues = validate_named_schema(schema)
    if issues:
        # Refused — the write never happens, the file is unchanged.
        return JSONResponse({"ok": False, "issues": issues}, status_code=400)

    # Guard: the target file must ALREADY exist (edit revises; it does not create —
    # that's the compiler's job). Find it via the same loader convention.
    schemas_dir = methodology_dir / "schemas"
    target: Path | None = None
    for yaml_file in sorted(schemas_dir.glob("*.yaml")):
        try:
            with yaml_file.open("r", encoding="utf-8") as f:
                for doc in yaml.safe_load_all(f):
                    if doc and doc.get("name") == schema_name:
                        target = yaml_file
                        break
        except yaml.YAMLError:
            continue
        if target is not None:
            break
    if target is None:
        raise HTTPException(
            status_code=404,
            detail=f"No existing schema file for '{schema_name}' in examples/{methodology}/schemas/",
        )

    with target.open("w", encoding="utf-8") as f:
        yaml.safe_dump(schema, f, sort_keys=False, allow_unicode=True, width=100)

    schemas = _load_schemas(methodology_dir)
    new_hash = node_review.schema_library_content_hash(schemas)
    state = node_review.data_model_state(methodology_dir, schemas)["state"]
    return JSONResponse({"ok": True, "content_hash": new_hash, "state": state})
