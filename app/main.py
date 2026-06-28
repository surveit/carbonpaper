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
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.runtime.runner import execute_run, prepare_run, resume_run, run_prepared
from app.runtime.preview import run_stage_preview, PreviewError, PREVIEWABLE_TYPES

from app import compiler  # the COMPILER feature (transcript → draft DAG)
from app.dag_schema import validate_schema_library  # named-schema (data-model) contract


# ─── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
EXAMPLES_DIR = REPO_ROOT / "examples"
COMPILATIONS_DIR = REPO_ROOT / "compilations"


# ─── App ─────────────────────────────────────────────────────────────────────

app = FastAPI(title="Methodology DAG")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ─── Helpers ─────────────────────────────────────────────────────────────────

# Stage-type → CSS class for DAG node + badges.
TYPE_CLASS = {
    "input_data": "input",
    "llm_transform": "llm",
    "python_transform": "python",
    "join": "join",
    "aggregate": "aggregate",
    "human_review_queue": "human",
    "publish": "publish",
}

TYPE_GLYPH = {
    "input_data": "▶",
    "llm_transform": "✦",
    "python_transform": "λ",
    "join": "⋈",
    "aggregate": "Σ",
    "human_review_queue": "👤",
    "publish": "📤",
}


def methodology_dirs() -> list[Path]:
    """A methodology dir has EITHER a compiled DAG OR a named-schema data model
    (or both). The data-model-only case is the point of named schemas: a
    methodology can exist as just a data model, before any DAG is authored."""
    if not EXAMPLES_DIR.exists():
        return []
    return [
        p for p in sorted(EXAMPLES_DIR.iterdir())
        if p.is_dir() and ((p / "compiled").is_dir() or (p / "schemas").is_dir())
    ]


# Named-schema kind → display. The four kinds are the distinction the
# DAG-derived data-model view could not express.
SCHEMA_KIND_CLASS = {
    "reference": "input",      # reuse existing palette
    "input": "aggregate",
    "computed": "python",
    "ground_truth": "human",
}
SCHEMA_KIND_GLYPH = {
    "reference": "📚",
    "input": "▶",
    "computed": "λ",
    "ground_truth": "✓",
}
SCHEMA_KIND_ORDER = ["reference", "input", "computed", "ground_truth"]


def list_methodologies() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for p in methodology_dirs():
        out.append({
            "name": p.name,
            "has_dag": (p / "compiled").is_dir(),
            "has_schemas": (p / "schemas").is_dir(),
        })
    return out


def load_schemas(methodology: str) -> list[dict[str, Any]]:
    """Load the named-schema data model from examples/<name>/schemas/*.yaml.
    Each file may hold one or many schemas (multi-doc YAML). Returns [] if the
    methodology has no data model."""
    schemas_dir = EXAMPLES_DIR / methodology / "schemas"
    if not schemas_dir.is_dir():
        return []
    schemas: list[dict[str, Any]] = []
    for yaml_file in sorted(schemas_dir.glob("*.yaml")):
        with yaml_file.open("r", encoding="utf-8") as f:
            try:
                for doc in yaml.safe_load_all(f):
                    if not doc:
                        continue
                    doc["_filename"] = yaml_file.name
                    schemas.append(doc)
            except yaml.YAMLError as exc:
                schemas.append({
                    "name": yaml_file.stem,
                    "title": f"[YAML ERROR] {yaml_file.name}",
                    "kind": "reference",
                    "notes": f"YAML parse error: {exc}",
                    "_filename": yaml_file.name,
                    "_error": True,
                })
    return schemas


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


def get_input_ids(stage: dict[str, Any]) -> list[str]:
    """v2 inputs are objects with id; older shapes might be plain strings."""
    inputs = stage.get("inputs") or []
    out = []
    for inp in inputs:
        if isinstance(inp, dict):
            iid = inp.get("id")
            if iid:
                out.append(iid)
        elif isinstance(inp, str):
            out.append(inp)
    return out


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


def build_schema_er_diagram(schemas: list[dict[str, Any]]) -> str:
    """Mermaid erDiagram from NAMED schemas (the data model), authored before any
    DAG. FK edges come from explicit column `references` (schema or schema.column)
    — a real graph, not the PK-name-collision heuristic the DAG-derived view used."""
    lines = ["erDiagram"]
    names = {s.get("name") for s in schemas if s.get("name")}

    for s in schemas:
        sid = s.get("name")
        if not sid:
            continue
        cols = s.get("columns") or []
        pk_set = set(s.get("primary_key") or [])
        lines.append(f"    {sid} {{")
        if not cols:
            lines.append(f"        any _ \"({s.get('kind', '')})\"")
        for col in cols:
            name = col.get("name", "")
            if not name:
                continue
            t = _safe_mermaid_type(col.get("type", "str"))
            marker = "PK" if name in pk_set else ("FK" if col.get("references") else "")
            label = col.get("description") or ""
            comment = f' "{label.replace(chr(34), chr(39))[:48]}"' if label else ""
            line = f"        {t} {name}"
            if marker:
                line += f" {marker}"
            lines.append(line + comment)
        lines.append("    }")

    # FK edges: a referencing column draws an edge from the target schema to this one.
    seen_edges: set[str] = set()
    for s in schemas:
        sid = s.get("name")
        for col in s.get("columns") or []:
            ref = col.get("references") if isinstance(col, dict) else None
            if not ref:
                continue
            target = ref.split(".", 1)[0].strip()
            if target not in names or target == sid:
                continue
            edge = f"    {target} ||--o{{ {sid} : {col.get('name')}"
            if edge not in seen_edges:
                seen_edges.add(edge)
                lines.append(edge)
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


def build_mermaid_graph(
    stages: list[dict[str, Any]],
    methodology: str,
    status_by_id: dict[str, str] | None = None,
) -> str:
    """Generate a Mermaid flowchart from stages.

    If status_by_id is given, each node gets a status glyph in its label and a
    coloured stroke override (green/amber/red/grey) layered over its type class.
    """
    status_glyph = {
        "ok": "✓",
        "running": "⟳",
        "validation_warnings": "⚠",
        "error": "✗",
        "awaiting_review": "👤",
        "pending": "…",
    }
    status_stroke = {
        "ok": ("#2a8a2a", "3px"),                 # complete → green
        "running": ("#e0a800", "3px"),            # in progress → yellow
        "validation_warnings": ("#cc8a00", "3px"),
        "error": ("#cc2a2a", "3px"),              # errored → red
        "awaiting_review": ("#2a6ac8", "4px"),
        "pending": ("#cfcfcf", "1px"),
    }
    lines = ["flowchart LR"]
    for s in stages:
        sid = s.get("id", s["_filename"])
        name = s.get("name", sid)
        stype = s.get("type", "?")
        glyph = TYPE_GLYPH.get(stype, "")
        klass = TYPE_CLASS.get(stype, "custom")
        notes_indicator = "⚠ " if s.get("compiler_notes") else ""
        eval_indicator = "📊" if s.get("eval") else ""
        review_indicator = "👤" if s.get("review") else ""
        small_line = f"{stype}".replace("_", " ")
        flags = " ".join(filter(None, [eval_indicator, review_indicator]))
        status = (status_by_id or {}).get(sid)
        status_prefix = f"{status_glyph.get(status, '')} " if status else ""
        # Use HTML in mermaid label
        label = (
            f'"<b>{status_prefix}{notes_indicator}{glyph} {name}</b>'
            f'<br/><span style=\'font-size:10px;color:#888\'>{small_line}</span>'
            + (f"<br/><span style='font-size:11px'>{flags}</span>" if flags else "")
            + '"'
        )
        lines.append(f"    {sid}[{label}]:::{klass}")
        lines.append(
            f'    click {sid} call loadStage("{sid}") "Open stage"'
        )
        if status and status in status_stroke:
            stroke, width = status_stroke[status]
            lines.append(f"    style {sid} stroke:{stroke},stroke-width:{width}")
    for s in stages:
        sid = s.get("id", s["_filename"])
        for upstream in get_input_ids(s):
            lines.append(f"    {upstream} --> {sid}")
    lines += [
        "    classDef input fill:#e8f4f8,stroke:#3a8ca8,color:#000",
        "    classDef llm fill:#fff4e6,stroke:#cc7a00,color:#000",
        "    classDef python fill:#eef2f7,stroke:#4a5e85,color:#000",
        "    classDef join fill:#f4ecfa,stroke:#7b3aa8,color:#000",
        "    classDef aggregate fill:#f0f0e6,stroke:#888533,color:#000",
        "    classDef human fill:#fce8f4,stroke:#c0399a,color:#000",
        "    classDef publish fill:#e8f8e8,stroke:#3aa83a,color:#000",
        "    classDef custom fill:#fde8e8,stroke:#cc3333,color:#000",
    ]
    return "\n".join(lines)


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


@app.get("/methodology/{methodology}", response_class=HTMLResponse)
async def methodology_view(request: Request, methodology: str):
    # Data-model-first: a methodology may have a schema library but no DAG yet.
    compiled_dir = EXAMPLES_DIR / methodology / "compiled"
    if not compiled_dir.is_dir() and (EXAMPLES_DIR / methodology / "schemas").is_dir():
        return RedirectResponse(url=f"/methodology/{methodology}/schemas")
    stages = load_stages(methodology)
    mermaid = build_mermaid_graph(stages, methodology)
    return templates.TemplateResponse(
        request,
        "methodology.html",
        {
            "methodology": methodology,
            "stages": stages,
            "mermaid": mermaid,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
            "get_input_ids": get_input_ids,
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


@app.get("/methodology/{methodology}/schemas", response_class=HTMLResponse)
async def schema_library_view(request: Request, methodology: str):
    """The data model: named schemas authored independent of (and before) the DAG."""
    schemas = load_schemas(methodology)
    if not schemas:
        raise HTTPException(status_code=404, detail=f"No data model (schemas/) for {methodology}")
    issues = validate_schema_library(schemas)
    has_dag = (EXAMPLES_DIR / methodology / "compiled").is_dir()
    return templates.TemplateResponse(
        request,
        "schema_library.html",
        {
            "methodology": methodology,
            "schemas": schemas,
            "er_diagram": build_schema_er_diagram(schemas),
            "issues": issues,
            "has_dag": has_dag,
            "kind_order": SCHEMA_KIND_ORDER,
            "kind_class": SCHEMA_KIND_CLASS,
            "kind_glyph": SCHEMA_KIND_GLYPH,
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
                "stages_total": len(manifest.get("stages", [])),
                "stages_ok": sum(1 for s in manifest.get("stages", []) if s.get("status") == "ok"),
                "stages_error": sum(1 for s in manifest.get("stages", []) if s.get("status") == "error"),
            })
    return entries


def _run_in_background(target, *args) -> None:
    """Run a (possibly slow, LLM-driven) execution off the event loop so the
    run page stays responsive and can poll live progress. Errors are recorded
    on the manifest by the runner itself; this just keeps the thread from dying
    silently."""
    def _wrapped():
        try:
            target(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_wrapped, daemon=True).start()


@app.post("/methodology/{methodology}/run")
async def trigger_run(methodology: str):
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    # Set up the run (writes an initial `running` manifest), kick off execution
    # in a background thread, and redirect immediately. The run page polls.
    prep = prepare_run(methodology_dir, REPO_ROOT)
    _run_in_background(run_prepared, prep)
    return RedirectResponse(
        url=f"/methodology/{methodology}/runs/{prep['run_id']}",
        status_code=303,
    )


@app.get("/methodology/{methodology}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, methodology: str):
    return templates.TemplateResponse(
        request,
        "runs_index.html",
        {"methodology": methodology, "runs": _list_runs(methodology)},
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
                   "err": _count("error"), "total": len(mstages),
                   "done": _count("ok") + _count("validation_warnings"),
                   "running": _count("running"), "pending": _count("pending"),
                   "awaiting": _count("awaiting_review")},
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

    # ── Recover the MODEL INPUT so the score is reviewable, not just visible. ──
    # The queue snapshot holds the scoring stage's OUTPUT (score + reasoning + ids);
    # the thing the model actually judged (the quote, the benchmark) lives in the
    # scoring stage's INPUT, one stage upstream. Join it back + render the prompt.
    from app.runtime.llm import render_prompt
    output_by_id = {s.get("stage_id"): s.get("output_path") for s in manifest.get("stages", [])}
    scored_ids = get_input_ids(stage_def)
    scored_def = next((s for s in stages if s.get("id") == scored_ids[0]), None) if scored_ids else None
    prompt_template = (scored_def.get("llm") or {}).get("prompt_template") if scored_def else None

    input_lookup: dict[tuple, dict[str, Any]] = {}
    join_keys: list[str] = []
    if scored_def and get_input_ids(scored_def):
        scored_in_id = get_input_ids(scored_def)[0]
        scored_in_decls = scored_def.get("inputs") or []
        pk = ((scored_in_decls[0].get("schema") or {}).get("primary_key")) if scored_in_decls else None
        in_path = output_by_id.get(scored_in_id)
        in_df = None
        if in_path:
            p = run_dir / in_path
            if p.exists():
                try:
                    in_df = pd.read_parquet(p) if p.suffix == ".parquet" else pd.read_csv(p)
                except Exception:  # noqa: BLE001
                    in_df = None
        if in_df is not None:
            cols = list(in_df.columns)
            join_keys = [k for k in (pk or []) if k in cols] or \
                [c for c in ("evidence_id", "entity_id", "doc_id", "id") if c in cols]
            if join_keys:
                for _, r in in_df.iterrows():
                    key = tuple(str(r[k]) for k in join_keys)
                    input_lookup[key] = {k: _display_cell(v) for k, v in r.items()}

    items: list[dict[str, Any]] = []
    if snapshot is not None:
        for _, row in snapshot.iterrows():
            h = row["content_hash"]
            existing = decision_by_hash.get(h)
            model_input = None
            rendered_prompt = None
            if input_lookup and join_keys and all(k in row.index for k in join_keys):
                model_input = input_lookup.get(tuple(str(row[k]) for k in join_keys))
                if model_input and prompt_template:
                    try:
                        rendered_prompt = render_prompt(prompt_template, model_input)
                    except Exception:  # noqa: BLE001
                        rendered_prompt = None
            items.append({
                "content_hash": h,
                "row": {k: _display_cell(v) for k, v in row.items()
                        if k not in ("content_hash", "decision", "modified_score",
                                     "reviewer", "reviewed_at")},
                "model_input": model_input,
                "rendered_prompt": rendered_prompt,
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
    """Resume/continue a run from where it stopped, re-running any stage that is
    NOT already complete (so this serves BOTH: a halted run after its review
    decisions, AND an ERRORED run after the bug is fixed — it re-runs the failed
    stage + downstream and reuses completed upstream outputs)."""
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


# ─── /compile — the COMPILER feature (transcript → draft DAG) ────────────────

RESEARCH_RUNS_DIR = EXAMPLES_DIR / "palm_osint" / "research_runs"


def list_transcripts() -> list[dict[str, Any]]:
    """Available research-run transcripts the compiler can distill."""
    if not RESEARCH_RUNS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(RESEARCH_RUNS_DIR.glob("*.jsonl")):
        # Suggest an out_name from the leading slug of the filename.
        slug = p.stem.split("__", 1)[0]
        out.append({
            "filename": p.name,
            "path": str(p.relative_to(REPO_ROOT)).replace("\\", "/"),
            "name": slug,
            "size_kb": round(p.stat().st_size / 1024),
        })
    return out


@app.get("/compile", response_class=HTMLResponse)
async def compilations_index(request: Request):
    """LIST of compilation objects (parallels runs_index). Each row is a persisted
    compilation; "New compilation" opens the form."""
    return templates.TemplateResponse(
        request,
        "compilations_index.html",
        {"compilations": compiler.list_compilations(COMPILATIONS_DIR)},
    )


@app.get("/compile/new", response_class=HTMLResponse)
async def compile_new_form(request: Request):
    """The compile FORM — pick a transcript, an out-name, a model. (The page that
    used to live at GET /compile.)"""
    return templates.TemplateResponse(
        request,
        "compile_new.html",
        {"transcripts": list_transcripts()},
    )


@app.post("/compile/new")
async def compile_new_submit(
    transcript: str = Form(...),
    out_name: str = Form(...),
    model: str = Form("sonnet"),
):
    """Run + PERSIST a compilation as a first-class object, then redirect to its
    detail page. The compile is a multi-minute LLM call, so — exactly like
    trigger_run — we write an initial `running` manifest, kick the work off in a
    background thread, and redirect immediately. The detail page polls."""
    transcript_path = (REPO_ROOT / transcript).resolve()
    # Confine to the research_runs dir (no arbitrary path read).
    if RESEARCH_RUNS_DIR.resolve() not in transcript_path.parents or not transcript_path.is_file():
        raise HTTPException(status_code=400, detail=f"Unknown transcript: {transcript}")

    safe_name = re.sub(r"[^a-z0-9_]", "_", out_name.strip().lower()) or "compiled"

    prep = compiler.prepare_compilation(
        COMPILATIONS_DIR, str(transcript_path), safe_name, model
    )
    _run_in_background(compiler.run_prepared_compilation, prep)
    return RedirectResponse(url=f"/compile/{prep['compilation_id']}", status_code=303)


@app.get("/compile/{compilation_id}/status")
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


@app.get("/compile/{compilation_id}", response_class=HTMLResponse)
async def compilation_detail(request: Request, compilation_id: str):
    """The COMPILATION OBJECT view (parallels run_detail). Three sections:
    (a) INPUT — transcript + parsed tool-sequence summary;
    (b) WHAT HAPPENED — the LLM prompt sent, the raw response, the validation result;
    (c) DAG OUTPUT — mermaid graph + stage table + methodology_raw.md."""
    try:
        comp = compiler.load_compilation(COMPILATIONS_DIR, compilation_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Compilation not found")

    stages = comp["stages"]
    mermaid = build_mermaid_graph(stages, comp["manifest"].get("name", compilation_id)) if stages else None

    return templates.TemplateResponse(
        request,
        "compile_detail.html",
        {
            "compilation_id": compilation_id,
            "manifest": comp["manifest"],
            "what_happened": comp["what_happened"],
            "stages": stages,
            "methodology_raw": comp["methodology_raw"],
            "error_text": comp["error_text"],
            "mermaid": mermaid,
            "type_class": TYPE_CLASS,
            "type_glyph": TYPE_GLYPH,
        },
    )
