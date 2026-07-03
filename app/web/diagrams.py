"""Pure presentation helpers: build the Mermaid flowchart and ER diagram from a
methodology's stages, plus the stage-type → CSS-class / glyph maps they share
with the templates. No I/O — stages in, diagram source out."""

from __future__ import annotations

from typing import Any

from app.web.loading import get_input_ids


# Stage-type → CSS class for DAG node + badges.
TYPE_CLASS = {
    "input_data": "input",
    "llm_transform": "llm",
    "python_row_function": "python",
    "python_frame_function": "python",
    "join": "join",
    "aggregate": "aggregate",
    "human_review_queue": "human",
    "publish": "publish",
}

TYPE_GLYPH = {
    "input_data": "▶",
    "llm_transform": "✦",
    "python_row_function": "🔂",
    "python_frame_function": "🧨",
    "join": "⋈",
    "aggregate": "Σ",
    "human_review_queue": "👤",
    "publish": "📤",
}


def _safe_mermaid_type(t: str) -> str:
    """Mermaid erDiagram is picky — strip brackets, slashes, etc."""
    return (
        t.replace("[", "_")
         .replace("]", "")
         .replace(" ", "_")
         .replace(":", "_")
         .replace("+", "p")
    ) or "any"


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


# Node-review BELIEF → stroke colour. Distinct from the type fill (classDef) and
# from run status: this is "do we trust HOW this node is modeled". Kept identical
# to the --belief-* palette in style.css so a legend chip equals the DAG stroke.
REVIEW_STROKE = {
    "approved": ("#2a8a2a", "3px"),       # trusted → green
    "unreviewed": ("#9aa3ad", "1.5px"),   # not yet reviewed → grey
    "rejected": ("#cc2a2a", "3px"),       # rejected → red
    "edited_stale": ("#cc8a00", "3px"),   # approved then edited → amber
}


def build_mermaid_graph(
    stages: list[dict[str, Any]],
    methodology: str,
    status_by_id: dict[str, str] | None = None,
    review_by_id: dict[str, str] | None = None,
) -> str:
    """Generate a Mermaid flowchart from stages.

    If status_by_id is given, each node gets a status glyph in its label and a
    coloured stroke override (green/amber/red/grey) layered over its type class.

    If review_by_id is given (stage_id → belief state in {approved, unreviewed,
    rejected, edited_stale}), each node's STROKE is coloured by belief instead —
    the type fill is unchanged, so stroke encodes trust while fill encodes type.
    When both are given, run status takes precedence (a live run's colour wins
    over the standing belief). When both are None, behaves exactly as before.
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
        # Stroke override: run status (if any) wins, else node-review belief.
        # Colour = BELIEF/STATUS, layered over the type class's fill.
        stroke_spec: tuple[str, str] | None = None
        if status and status in status_stroke:
            stroke_spec = status_stroke[status]
        else:
            belief = (review_by_id or {}).get(sid)
            if belief and belief in REVIEW_STROKE:
                stroke_spec = REVIEW_STROKE[belief]
        if stroke_spec is not None:
            stroke, width = stroke_spec
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
