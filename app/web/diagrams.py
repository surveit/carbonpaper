"""Pure presentation helpers: build the Mermaid flowchart and ER diagram from a
project's stages. No I/O — stages in, diagram source out."""

from __future__ import annotations

from typing import Any

from app.models import Stage, StageBase
from app.core.run_status import StageStatus


# Stage-type → CSS class for workflow node + badges, and its glyph. Every StageType
# must appear in both maps: an unmapped type falls back to `custom`, the red badge
# palette that elsewhere means error. tests/arch/test_stage_type_presentation.py
# fails when one is missing. Read by the exported review packet too, which vendors
# this app's stylesheet so a packet looks like the app it came from.
TYPE_CLASS = {
    "input_data": "input",
    "llm_transform": "llm",
    "python_row_function": "python",
    "python_frame_function": "python",
    "starlark_row_function": "python",
    "enrich": "join",
    "expand": "join",
    "aggregate": "aggregate",
    "human_review_queue": "human",
    "publish": "publish",
    # Row-set operations: union stacks frames, filter_rows drops subject rows.
    "union": "rowset",
    "filter_rows": "rowset",
}

TYPE_GLYPH = {
    "input_data": "⬆️",
    "llm_transform": "✨",
    "python_row_function": "🔂",
    "python_frame_function": "🧨",
    "starlark_row_function": "🛡️",
    "enrich": "🔗",
    "expand": "🌿",
    "aggregate": "📊",
    "human_review_queue": "👤",
    "publish": "📤",
    "union": "➕",
    "filter_rows": "🔽",
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
        lines.extend(_render_table_node_block(s))
    lines.extend(_collect_table_fk_edges(schemas, names))

    # One neutral surface for every schema kind, matching the .type-tag chip
    # beside it: a kind is not a state, so it takes no colour, and
    # SCHEMA_KIND_GLYPH is what says which kind a node is. _NODE_SURFACE is the
    # same decision on the workflow graph; both are palette.css's --bg / --border
    # / --fg, pinned by tests/arch/test_status_colour_contract.py.
    lines += [
        f"    classDef {kind} {_NODE_SURFACE}"
        for kind in sorted(set(SCHEMA_KIND_CLASS.values()) | {"custom"})
    ]
    return "\n".join(lines)


def _render_table_node_block(s: dict[str, Any]) -> list[str]:
    """One schema's flowchart node + click handler: name + title (dropped
    when identical to the name), coloured by kind. [] for a nameless
    schema."""
    sid = s.get("name")
    if not sid:
        return []
    klass = SCHEMA_KIND_CLASS.get(s.get("kind", ""), "custom")
    title = (s.get("title") or "").strip().replace('"', "'")[:48]
    label = f'"<b>{sid}</b>'
    if title and title != sid:
        label += f"<br/><span style='font-size:10px;color:#5c6169'>{title}</span>"
    label += '"'
    return [
        f"    {sid}[{label}]:::{klass}",
        f'    click {sid} call focusSchema("{sid}") "Open columns"',
    ]


def _collect_table_fk_edges(schemas: list[dict[str, Any]], names: set[Any]) -> list[str]:
    """One deduplicated table-level edge per referencing column: referenced
    schema --> the schema whose column carries the key. Same extraction as
    the ER view's `_collect_er_fk_edges`, drawn at table granularity."""
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
            edge = f"    {target} --> {sid}"
            if edge not in seen_edges:
                seen_edges.add(edge)
                edges.append(edge)
    return edges


def _node_view(s: Stage | dict[str, Any]) -> dict[str, Any]:
    """Read the label/edge fields a node needs off EITHER a typed Stage or a raw
    draft dict, into a uniform dict. The workflow view passes validated Stages; the
    project shell's workflow section passes draft dicts straight off disk (which may
    not yet validate) — both render the same graph. `input_ids` normalises the
    `inputs` shorthand (bare id string or {id: ...}) the Stage model also accepts."""
    if isinstance(s, StageBase):
        return {
            "id": s.id,
            "description": s.description,
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
        "description": s.get("description") or "",
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
) -> str:
    """Mermaid flowchart from typed Stages or draft dicts. Stroke = run status."""
    nodes = [_node_view(s) for s in stages]
    lines = ["flowchart LR"]
    for n in nodes:
        lines.extend(_render_workflow_node_lines(n, status_by_id or {}))
    for n in nodes:
        sid = n["id"]
        for upstream in n["input_ids"]:
            lines.append(f"    {upstream} --> {sid}")
    lines.extend(_render_node_classdefs())
    return "\n".join(lines)


# One neutral surface for every stage type: the node's glyph and its type-name
# subtitle say which type it is, leaving the stroke as the node's only colour —
# the run status. Values are palette.css's --bg / --border / --fg, so a node
# sits on the same sheet as the rest of the page.
_NODE_SURFACE = "fill:#fbfbfb,stroke:#e1e1e1,color:#24272b"
# What TYPE_CLASS falls back to for a stage type it does not map.
_FALLBACK_NODE_CLASS = "custom"


def _render_node_classdefs() -> list[str]:
    """A `classDef` per class `_render_workflow_node_lines` can emit, all the same surface."""
    classes = sorted(set(TYPE_CLASS.values()) | {_FALLBACK_NODE_CLASS})
    return [f"    classDef {name} {_NODE_SURFACE}" for name in classes]


# Keyed by StageStatus but typed `dict[str, ...]`: status_by_id (below) carries
# plain strings read back off the JSON manifest, and a StrEnum member hashes/
# equals its bare string, so lookups by that plain string still hit.
_STATUS_GLYPH: dict[str, str] = {
    StageStatus.OK: "✓",
    StageStatus.RUNNING: "⟳",
    StageStatus.VALIDATION_WARNINGS: "⚠",
    StageStatus.ERROR: "✗",
    StageStatus.AWAITING_REVIEW: "👤",
    StageStatus.CANCELLED: "✖",
    StageStatus.PENDING: "…",
}
# Run STATUS → stroke colour. Seven statuses, five colours: _STATUS_GLYPH above
# carries the distinction the shared colour drops (running ⟳ vs warnings ⚠,
# cancelled ✖ vs pending …). Every colour here is one of the five --state-*
# properties in palette.css, enforced by tests/arch/test_status_colour_contract.py.
_STATUS_STROKE: dict[str, tuple[str, str]] = {
    StageStatus.OK: ("#3b6c39", "3px"),                    # done
    StageStatus.RUNNING: ("#8a602e", "3px"),               # warning
    StageStatus.VALIDATION_WARNINGS: ("#8a602e", "3px"),   # warning
    StageStatus.ERROR: ("#8c4538", "3px"),                 # failed
    StageStatus.AWAITING_REVIEW: ("#35538d", "4px"),       # needs a human
    StageStatus.CANCELLED: ("#787d86", "3px"),             # idle
    StageStatus.PENDING: ("#787d86", "1px"),               # idle
}


def _render_workflow_node_lines(
    n: dict[str, Any], status_by_id: dict[str, str]
) -> list[str]:
    """One node's flowchart declaration, click handler (always the one dispatcher,
    static/diagram_nodes.js), and (if a status applies) a `style` line."""
    sid = n["id"]
    stype = n["type"]
    status = status_by_id.get(sid)
    label = _build_workflow_node_label(n, status)
    lines = [
        f"    {sid}[{label}]:::{TYPE_CLASS.get(stype, _FALLBACK_NODE_CLASS)}",
        f'    click {sid} call dvNode("{sid}") "{_build_node_tooltip(n)}"',
    ]
    stroke_line = _resolve_stroke_line(sid, status)
    if stroke_line is not None:
        lines.append(stroke_line)
    return lines


def _build_workflow_node_label(n: dict[str, Any], status: str | None) -> str:
    """The node's HTML label: glyphs, the stage id — its one name — then type and flags."""
    sid = n["id"]
    stype = n["type"]
    glyph = TYPE_GLYPH.get(stype, "")
    notes_indicator = "⚠ " if n["has_notes"] else ""
    eval_indicator = "📊" if n["has_eval"] else ""
    review_indicator = "👤" if n["has_review"] else ""
    small_line = f"{stype}".replace("_", " ")
    flags = " ".join(filter(None, [eval_indicator, review_indicator]))
    status_prefix = f"{_STATUS_GLYPH.get(status, '')} " if status else ""
    return (
        f'"<b>{status_prefix}{notes_indicator}{glyph} {sid}</b>'
        f'<br/><span style=\'font-size:10px;color:#5c6169\'>{small_line}</span>'
        + (f"<br/><span style='font-size:11px'>{flags}</span>" if flags else "")
        + '"'
    )


def _build_node_tooltip(n: dict[str, Any]) -> str:
    # A `"` would close the mermaid string early; a draft mid-edit may carry no
    # description at all.
    return (n["description"] or "Open stage").replace('"', "'")


def _resolve_stroke_line(sid: str, status: str | None) -> str | None:
    """The `style {sid} stroke:...` override line, or None for the type
    class's default stroke."""
    if not status or status not in _STATUS_STROKE:
        return None
    stroke, width = _STATUS_STROKE[status]
    return f"    style {sid} stroke:{stroke},stroke-width:{width}"
