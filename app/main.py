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
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.runtime.runner import execute_run, resume_run


# ─── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
EXAMPLES_DIR = REPO_ROOT / "examples"


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


def list_methodologies() -> list[str]:
    if not EXAMPLES_DIR.exists():
        return []
    return [
        p.name
        for p in sorted(EXAMPLES_DIR.iterdir())
        if p.is_dir() and (p / "compiled").is_dir()
    ]


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
        "validation_warnings": "⚠",
        "error": "✗",
        "awaiting_review": "👤",
        "pending": "…",
    }
    status_stroke = {
        "ok": ("#2a8a2a", "3px"),
        "validation_warnings": ("#cc8a00", "3px"),
        "error": ("#cc2a2a", "3px"),
        "awaiting_review": ("#2a6ac8", "4px"),
        "pending": ("#aaaaaa", "1px"),
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


@app.post("/methodology/{methodology}/run")
async def trigger_run(methodology: str):
    methodology_dir = EXAMPLES_DIR / methodology
    if not methodology_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"No methodology '{methodology}'")
    manifest = execute_run(methodology_dir, REPO_ROOT)
    return RedirectResponse(
        url=f"/methodology/{methodology}/runs/{manifest['run_id']}",
        status_code=303,
    )


@app.get("/methodology/{methodology}/runs", response_class=HTMLResponse)
async def runs_index(request: Request, methodology: str):
    return templates.TemplateResponse(
        request,
        "runs_index.html",
        {"methodology": methodology, "runs": _list_runs(methodology)},
    )


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
            "type_glyph": TYPE_GLYPH,
            "type_class": TYPE_CLASS,
        },
    )


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
                "row": {k: ("" if pd.isna(v) else v) for k, v in row.items()
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
    try:
        resume_run(methodology_dir, run_id, REPO_ROOT)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return RedirectResponse(
        url=f"/methodology/{methodology}/runs/{run_id}",
        status_code=303,
    )
