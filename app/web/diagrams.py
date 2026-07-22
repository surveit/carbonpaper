"""Pure presentation helpers: build the Mermaid flowchart and ER diagram from a
project's stages, plus the stage-type → CSS-class / glyph maps they share
with the templates. No I/O — stages in, diagram source out."""

from __future__ import annotations

from typing import Any

from app.models import Stage
from app.core.run_status import StageStatus


# Stage-type → CSS class for workflow node + badges.
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
    "input_data": "⬆️",
    "llm_transform": "✨",
    "python_row_function": "🔂",
    "python_frame_function": "🧨",
    "join": "🔗",
    "aggregate": "📊",
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


# ─── Named-schema data model (the ER view of schemas/, not stages) ────────────
# Schema-kind → CSS class / glyph, shared with the data-model section template.
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


def build_schema_er_diagram(schemas: list[dict[str, Any]]) -> str:
    """Mermaid erDiagram from NAMED schemas (the data model). FK edges come from
    explicit column `references` (schema or schema.column) — a real graph, not a
    PK-name-collision heuristic. An empty-column schema still renders as an entity so
    the reader sees it exists."""
    names = {s.get("name") for s in schemas if s.get("name")}
    lines = ["erDiagram"]
    for s in schemas:
        lines.extend(_render_er_entity_block(s))
    lines.extend(_collect_er_fk_edges(schemas, names))
    return "\n".join(lines)


def _render_er_entity_block(s: dict[str, Any]) -> list[str]:
    """One schema's `erDiagram` entity block: its `{ ... }` braces, an `any`
    placeholder row if it declares no columns, else one row per column."""
    sid = s.get("name")
    if not sid:
        return []
    cols = s.get("columns") or []
    pk_set = set(s.get("primary_key") or [])
    lines = [f"    {sid} {{"]
    if not cols:
        lines.append(f"        any _ \"({s.get('kind', '')})\"")
    for col in cols:
        line = _render_er_column_row(col, pk_set)
        if line is not None:
            lines.append(line)
    lines.append("    }")
    return lines


def _render_er_column_row(col: dict[str, Any], pk_set: set[Any]) -> str | None:
    """One column's row inside an entity block — type, name, PK/FK marker,
    and a truncated, quote-escaped description comment — or `None` for a
    column with no name."""
    name = col.get("name", "")
    if not name:
        return None
    t = _safe_mermaid_type(col.get("type", "str"))
    marker = "PK" if name in pk_set else ("FK" if col.get("references") else "")
    label = col.get("description") or ""
    comment = f' "{label.replace(chr(34), chr(39))[:48]}"' if label else ""
    line = f"        {t} {name}"
    if marker:
        line += f" {marker}"
    return line + comment


def _collect_er_fk_edges(schemas: list[dict[str, Any]], names: set[Any]) -> list[str]:
    """One deduplicated `erDiagram` edge per referencing column: a referencing
    column draws an edge from the target schema to this one, skipping a
    reference to an unknown schema or to the referencing schema itself."""
    edges: list[str] = []
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
                edges.append(edge)
    return edges


def build_schema_table_graph(schemas: list[dict[str, Any]]) -> str:
    """Mermaid flowchart of the data model at TABLE level: one node per named
    schema (name + title, coloured by kind), one edge per foreign-key reference.
    The columns-free companion to build_schema_er_diagram — same deterministic
    edge source (explicit column `references` only), so it makes no dataflow
    claim: an edge A --> B means B carries a key pointing at A, and a table that
    reads another without carrying its key (e.g. a roll-up) shows no edge.
    Nodes click through to focusSchema(name) so the page can open that schema's
    reference detail."""
    lines = ["flowchart LR"]
    names = {s.get("name") for s in schemas if s.get("name")}

    for s in schemas:
        sid = s.get("name")
        if not sid:
            continue
        klass = SCHEMA_KIND_CLASS.get(s.get("kind", ""), "custom")
        title = (s.get("title") or "").strip().replace('"', "'")[:48]
        label = f'"<b>{sid}</b>'
        if title and title != sid:
            label += f"<br/><span style='font-size:10px;color:#888'>{title}</span>"
        label += '"'
        lines.append(f"    {sid}[{label}]:::{klass}")
        lines.append(f'    click {sid} call focusSchema("{sid}") "Open columns"')

    # FK edges: referenced schema --> the schema whose column carries the key.
    # Same extraction as the ER view, deduped at table level.
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
            edge = f"    {target} --> {sid}"
            if edge not in seen_edges:
                seen_edges.add(edge)
                lines.append(edge)

    # Same kind-fill palette as the workflow graph's classDefs, keyed through
    # SCHEMA_KIND_CLASS so a node here matches the kind's .type-tag chip.
    lines += [
        "    classDef input fill:#e8f4f8,stroke:#3a8ca8,color:#000",
        "    classDef aggregate fill:#f0f0e6,stroke:#888533,color:#000",
        "    classDef python fill:#eef2f7,stroke:#4a5e85,color:#000",
        "    classDef human fill:#fce8f4,stroke:#c0399a,color:#000",
        "    classDef custom fill:#fde8e8,stroke:#cc3333,color:#000",
    ]
    return "\n".join(lines)


# Node-review BELIEF → stroke colour. Distinct from the type fill (classDef) and
# from run status: this is "do we trust HOW this node is modeled". Kept identical
# to the --belief-* palette in style.css so a legend chip equals the workflow stroke.
REVIEW_STROKE = {
    "approved": ("#2a8a2a", "3px"),       # trusted → green
    "unreviewed": ("#9aa3ad", "1.5px"),   # not yet reviewed → grey
    "rejected": ("#cc2a2a", "3px"),       # rejected → red
    "edited_stale": ("#cc8a00", "3px"),   # approved then edited → amber
}


def _node_view(s: Stage | dict[str, Any]) -> dict[str, Any]:
    """Read the label/edge fields a node needs off EITHER a typed Stage or a raw
    draft dict, into a uniform dict. The workflow view passes validated Stages; the
    project shell's workflow section passes draft dicts straight off disk (which may
    not yet validate) — both render the same graph. `input_ids` normalises the
    `inputs` shorthand (bare id string or {id: ...}) the Stage model also accepts."""
    if isinstance(s, Stage):
        return {
            "id": s.id,
            "name": s.name,
            "type": s.type,
            "has_notes": bool(s.compiler_notes),
            "has_eval": s.eval is not None,
            "has_review": s.review is not None,
            "input_ids": s.input_ids,
        }
    input_ids: list[str] = []
    for inp in s.get("inputs") or []:
        if isinstance(inp, str):
            input_ids.append(inp)
        elif isinstance(inp, dict) and inp.get("id"):
            input_ids.append(str(inp["id"]))
    sid = s.get("id") or s.get("_filename") or "?"
    return {
        "id": sid,
        "name": s.get("name") or sid,
        "type": s.get("type") or "?",
        "has_notes": bool(s.get("compiler_notes")),
        "has_eval": bool(s.get("eval")),
        "has_review": bool(s.get("review")),
        "input_ids": input_ids,
    }


def build_mermaid_graph(
    stages: list[Stage] | list[dict[str, Any]],
    project: str,
    status_by_id: dict[str, str] | None = None,
    review_by_id: dict[str, str] | None = None,
) -> str:
    """Generate a Mermaid flowchart from stages (typed Stages or raw draft dicts).

    If status_by_id is given, each node gets a status glyph in its label and a
    coloured stroke override (green/amber/red/grey) layered over its type class.

    If review_by_id is given (stage_id → belief state in {approved, unreviewed,
    rejected, edited_stale}), each node's STROKE is coloured by belief instead —
    the type fill is unchanged, so stroke encodes trust while fill encodes type.
    When both are given, run status takes precedence (a live run's colour wins
    over the standing belief). When both are None, behaves exactly as before.
    """
    # Keyed by StageStatus but typed `dict[str, ...]`: status_by_id (below) carries
    # plain strings read back off the JSON manifest, and a StrEnum member hashes/
    # equals its bare string, so lookups by that plain string still hit.
    status_glyph: dict[str, str] = {
        StageStatus.OK: "✓",
        StageStatus.RUNNING: "⟳",
        StageStatus.VALIDATION_WARNINGS: "⚠",
        StageStatus.ERROR: "✗",
        StageStatus.AWAITING_REVIEW: "👤",
        StageStatus.CANCELLED: "✖",
        StageStatus.PENDING: "…",
    }
    status_stroke: dict[str, tuple[str, str]] = {
        StageStatus.OK: ("#2a8a2a", "3px"),                 # complete → green
        StageStatus.RUNNING: ("#e0a800", "3px"),            # in progress → yellow
        StageStatus.VALIDATION_WARNINGS: ("#cc8a00", "3px"),
        StageStatus.ERROR: ("#cc2a2a", "3px"),              # errored → red
        StageStatus.AWAITING_REVIEW: ("#2a6ac8", "4px"),
        StageStatus.CANCELLED: ("#8a8a8a", "3px"),          # cancelled → grey
        StageStatus.PENDING: ("#cfcfcf", "1px"),
    }
    nodes = [_node_view(s) for s in stages]
    lines = ["flowchart LR"]
    for n in nodes:
        sid = n["id"]
        name = n["name"]
        stype = n["type"]
        glyph = TYPE_GLYPH.get(stype, "")
        klass = TYPE_CLASS.get(stype, "custom")
        notes_indicator = "⚠ " if n["has_notes"] else ""
        eval_indicator = "📊" if n["has_eval"] else ""
        review_indicator = "👤" if n["has_review"] else ""
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
    for n in nodes:
        sid = n["id"]
        for upstream in n["input_ids"]:
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
