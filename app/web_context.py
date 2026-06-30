"""
web_context.py — shared web primitives for the FastAPI app.

`main.py` owns the `app` object and the run/methodology/review routes; the compiler
feature's routes live in `app/pages.py` (pages) and `app/api/compile.py` (actions).
Both need the same handful of singletons and helpers — the Jinja templates, the
path constants, the DAG-rendering helper, the background-thread runner. They live
here so the route modules can import them WITHOUT importing `main` (which would be
a circular import, since `main` includes their routers).

This module imports nothing from `app.main` / `app.pages` / `app.api` — it sits
below them in the dependency graph.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

import yaml
from fastapi.templating import Jinja2Templates


# ─── Paths ───────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
EXAMPLES_DIR = REPO_ROOT / "examples"
COMPILATIONS_DIR = REPO_ROOT / "compilations"
# Unstructured inputs the compiler can distill (transcripts, notes, prose).
RESEARCH_RUNS_DIR = EXAMPLES_DIR / "palm_osint" / "research_runs"


# ─── Templates ─────────────────────────────────────────────────────────────────

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ─── Stage-type → presentation ───────────────────────────────────────────────

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


# ─── Shared helpers ──────────────────────────────────────────────────────────

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


def build_mermaid_graph(
    stages: list[dict[str, Any]],
    methodology: str,
    status_by_id: dict[str, str] | None = None,
    review_by_id: dict[str, str] | None = None,
) -> str:
    """Generate a Mermaid flowchart from stages.

    If status_by_id is given, each node gets a status glyph in its label and a
    coloured stroke override (green/amber/red/grey) layered over its type class.

    If review_by_id is given (maps stage id → approval state in
    {approved, unreviewed, rejected, edited_stale}, per node_review), the node's
    PRIMARY FILL is set by belief — green/grey/red/amber — so the DAG reads as
    "what do we trust" at a glance. Node TYPE stays visible via the existing
    TYPE_GLYPH in the label (NOT via fill, which belief now owns). Any run-status
    stroke still layers on top, so a node can show belief (fill) and run state
    (stroke) at once.
    """
    review_style = {
        "approved": ("#e7f6e7", "#2a8a2a"),       # trusted → green
        "unreviewed": ("#eceff3", "#9aa3ad"),     # not yet looked at → grey
        "rejected": ("#fbeaea", "#cc2a2a"),       # we don't believe it → red
        "edited_stale": ("#fdf3e0", "#cc8a00"),   # approved then edited → amber
    }
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
        # Belief fill (review state) layered over the type class. Emitted BEFORE
        # any status stroke so the run-status stroke override wins on `stroke`
        # while this keeps `fill` — same per-node `style` mechanism the run page
        # already uses (run_detail.html:123).
        review_state = (review_by_id or {}).get(sid)
        if review_state and review_state in review_style:
            fill, stroke = review_style[review_state]
            lines.append(
                f"    style {sid} fill:{fill},stroke:{stroke},stroke-width:2px"
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


# ─── Named-schema data model (the "data model first" view) ───────────────────
# A methodology's data model is a set of NAMED schemas authored in
# examples/<name>/schemas/ BEFORE the DAG. These load + render them (kind palette,
# ER diagram) and are shared by the methodology data-model route (app.main) and the
# gated-compile data-model step (app.api.compile) so both render the model the same
# way. (Moved here out of app.api.compile so app.main can reuse them without an
# import cycle.)

# Named-schema kind → display (the four-kind data-model palette).
SCHEMA_KIND_CLASS = {
    "reference": "input",
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


def _load_schemas(methodology_dir: Path) -> list[dict[str, Any]]:
    """Load the named-schema data model from <methodology_dir>/schemas/*.yaml.
    Each file may hold one or many schemas (multi-doc YAML). Returns [] if the
    methodology has no data model yet."""
    schemas_dir = methodology_dir / "schemas"
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


def _safe_schema_mermaid_type(t: str) -> str:
    """Mermaid erDiagram is picky — strip brackets, slashes, etc."""
    return (
        t.replace("[", "_")
         .replace("]", "")
         .replace(" ", "_")
         .replace(":", "_")
         .replace("+", "p")
    ) or "any"


def _build_schema_er_diagram(schemas: list[dict[str, Any]]) -> str:
    """Mermaid erDiagram from NAMED schemas (the data model). FK edges come from
    explicit column `references` (schema or schema.column) — a real graph, not a
    PK-name-collision heuristic."""
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
            t = _safe_schema_mermaid_type(col.get("type", "str"))
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


def _run_in_background(target, *args) -> None:
    """Run a (possibly slow, LLM-driven) execution off the event loop so the
    page stays responsive and can poll live progress. Errors are recorded on the
    manifest by the worker itself; this just keeps the thread from dying silently."""
    def _wrapped():
        try:
            target(*args)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    threading.Thread(target=_wrapped, daemon=True).start()


def list_inputs() -> list[dict[str, Any]]:
    """Unstructured inputs the compiler can distill: transcripts (.jsonl), notes /
    methodology (.md), or prose (.txt) under RESEARCH_RUNS_DIR. Each is fed to the
    model as prose regardless of extension."""
    if not RESEARCH_RUNS_DIR.is_dir():
        return []
    out: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for pattern in ("*.jsonl", "*.md", "*.txt"):
        for p in RESEARCH_RUNS_DIR.glob(pattern):
            if p in seen:
                continue
            seen.add(p)
            # Suggest an out_name from the leading slug of the filename.
            slug = p.stem.split("__", 1)[0]
            out.append({
                "filename": p.name,
                "path": str(p.relative_to(REPO_ROOT)).replace("\\", "/"),
                "name": slug,
                "size_kb": round(p.stat().st_size / 1024),
            })
    out.sort(key=lambda d: d["filename"])
    return out
