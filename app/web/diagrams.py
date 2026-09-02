"""Pure presentation helpers: build the Mermaid flowchart and ER diagram from a
project's stages. No I/O — stages in, diagram source out."""

from __future__ import annotations

from typing import Any, Sequence

from app.models import AbstractStage
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
    "starlark_filter_rows": "rowset",
    "enrich": "join",
    "expand": "join",
    "aggregate": "aggregate",
    "human_review_queue": "human",
    "report": "report",
    # Row-set operations: union stacks frames, filter_rows drops subject rows.
    "union": "rowset",
    "filter_rows": "rowset",
    "explode": "rowset",
    "dedupe": "rowset",
    "sort_rank": "rowset",
}

TYPE_GLYPH = {
    "input_data": "⬆️",
    "llm_transform": "✨",
    "python_row_function": "⚠️",
    "python_frame_function": "⚠️",
    "starlark_row_function": "⚙️",
    "starlark_filter_rows": "🔽",
    "enrich": "🔗",
    "expand": "🌿",
    "aggregate": "📊",
    "human_review_queue": "👤",
    "report": "📤",
    "union": "➕",
    "filter_rows": "⚠️",
    "explode": "🌱",
    "dedupe": "🧹",
    "sort_rank": "🔢",
}

# What the type tag SAYS. The slug is the id the config, the manifest and the
# authoring surfaces use; a reader of the run page is a journalist, so the tag
# reads as English instead. "human" is not a word this vocabulary uses on its
# own — the queue is named for what it holds. An unmapped type falls back to its
# slug, which tests/arch/test_stage_type_presentation.py fails on.
TYPE_LABEL = {
    "input_data": "input",
    "llm_transform": "model transform",
    "python_row_function": "dangerously run code",
    "python_frame_function": "dangerously run code on the table",
    "starlark_row_function": "run code",
    "starlark_filter_rows": "filter rows",
    "enrich": "enrich",
    "expand": "expand",
    "aggregate": "aggregate",
    "human_review_queue": "review queue",
    "report": "report",
    "union": "union",
    "filter_rows": "dangerously filter rows",
    "explode": "explode",
    "dedupe": "dedupe",
    "sort_rank": "sort and rank",
}


def _safe_mermaid_type(t: str) -> str:
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
    names = {s.get("name") for s in schemas if s.get("name")}
    lines = ["erDiagram"]
    for s in schemas:
        lines.extend(_render_er_entity_block(s))
    lines.extend(_collect_er_fk_edges(schemas, names))
    return "\n".join(lines)


def _render_er_entity_block(s: dict[str, Any]) -> list[str]:
    sid = s.get("name")
    if not sid:
        return []
    cols = s.get("columns") or []
    pk_set = set(s.get("primary_key") or [])
    lines = [f"    {sid} {{"]
    if not cols:
        kind = s.get("kind")
        lines.append(f'        any _ "({kind})"' if kind else "        any _")
    for col in cols:
        line = _render_er_column_row(col, pk_set)
        if line is not None:
            lines.append(line)
    lines.append("    }")
    return lines


def _render_er_column_row(col: dict[str, Any], pk_set: set[Any]) -> str | None:
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
    """An edge A --> B means B carries a key pointing at A — never a dataflow claim."""
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
    sid = s.get("name")
    if not sid:
        return []
    title = (s.get("title") or "").strip().replace('"', "'")[:48]
    label = f'"<b>{sid}</b>'
    if title and title != sid:
        label += f"<br/><span style='font-size:10px;color:#5c6169'>{title}</span>"
    label += '"'
    return [
        f"    {sid}[{label}]{_schema_node_class(s.get('kind'))}",
        f'    click {sid} call focusSchema("{sid}") "Open columns"',
    ]


def _schema_node_class(kind: Any) -> str:
    if not kind:
        # No class at all: `custom` is where an UNRECOGNISED kind lands, and borrowing
        # it for a schema that declares none would draw a fifth kind nobody chose.
        return ""
    return f":::{SCHEMA_KIND_CLASS.get(kind, 'custom')}"


def _collect_table_fk_edges(schemas: list[dict[str, Any]], names: set[Any]) -> list[str]:
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


def _node_view(s: AbstractStage | dict[str, Any]) -> dict[str, Any]:
    if isinstance(s, AbstractStage):
        return {
            "id": s.id,
            "description": s.description,
            "type": s.type,
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
        "has_eval": bool(s.get("eval")),
        "has_review": bool(s.get("review")),
        "input_ids": input_ids,
    }


def build_mermaid_graph(
    stages: Sequence[AbstractStage] | Sequence[dict[str, Any]],
    project_id: str,
    status_by_id: dict[str, str] | None = None,
) -> str:
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
_NODE_SURFACE = "fill:#f6f5f0,stroke:#e6e4dc,color:#24272b"
# What TYPE_CLASS falls back to for a stage type it does not map.
_FALLBACK_NODE_CLASS = "custom"


def _render_node_classdefs() -> list[str]:
    classes = sorted(set(TYPE_CLASS.values()) | {_FALLBACK_NODE_CLASS})
    return [f"    classDef {name} {_NODE_SURFACE}" for name in classes]


# Keyed by StageStatus but typed `dict[str, ...]`: status_by_id (below) carries
# plain strings read back off the JSON manifest, and a StrEnum member hashes/
# equals its bare string, so lookups by that plain string still hit.
_STATUS_GLYPH: dict[str, str] = {
    # OK has none: its green stroke already says the stage finished.
    StageStatus.RUNNING: "⟳",
    StageStatus.VALIDATION_WARNINGS: "⚠",
    StageStatus.ERROR: "✗",
    StageStatus.AWAITING_REVIEW: "👤",
    StageStatus.CANCELLED: "✖",
    StageStatus.PENDING: "…",
}
# Run STATUS → stroke colour. Seven statuses, four colours: _STATUS_GLYPH above
# carries the distinction the shared colour drops (cancelled ✖ vs pending …, both
# idle). Every colour here is one of the five --state-* properties in palette.css,
# enforced by tests/arch/test_status_colour_contract.py.
_STATUS_STROKE: dict[str, tuple[str, str]] = {
    StageStatus.OK: ("#2f6d30", "3px"),                    # done
    StageStatus.RUNNING: ("#787d86", "3px"),               # idle
    StageStatus.VALIDATION_WARNINGS: ("#8b602c", "3px"),   # warning
    StageStatus.ERROR: ("#934133", "3px"),                 # failed
    StageStatus.AWAITING_REVIEW: ("#007a93", "4px"),       # needs a human
    StageStatus.CANCELLED: ("#787d86", "3px"),             # idle
    StageStatus.PENDING: ("#787d86", "1px"),               # idle
}
# A running stage has reached no verdict, so it spends no verdict hue — it strokes
# idle, and the dashes are what say it is still moving. A mermaid `style` cannot
# animate, so this is the still form of the stage strip's moving stripe.
_STATUS_DASH: dict[str, str] = {StageStatus.RUNNING: "6 4"}


def _render_workflow_node_lines(
    n: dict[str, Any], status_by_id: dict[str, str]
) -> list[str]:
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


# Always two lines: a third for the flags made a flagged node taller than the rest.
def _build_workflow_node_label(n: dict[str, Any], status: str | None) -> str:
    stype = n["type"]
    title = " ".join(filter(None, [
        _STATUS_GLYPH.get(status or "", ""), TYPE_GLYPH.get(stype, ""), n["id"],
    ]))
    subtitle = " ".join(filter(None, [
        TYPE_LABEL.get(stype, stype),
        "📊" if n["has_eval"] else "",
        "👤" if n["has_review"] else "",
    ]))
    return (
        f'"<b>{title}</b>'
        f'<br/><span style=\'font-size:10px;color:#5c6169\'>{subtitle}</span>"'
    )


def _build_node_tooltip(n: dict[str, Any]) -> str:
    # A `"` would close the mermaid string early; a draft mid-edit may carry no
    # description at all.
    return (n["description"] or "Open stage").replace('"', "'")


def _resolve_stroke_line(sid: str, status: str | None) -> str | None:
    if not status or status not in _STATUS_STROKE:
        return None
    stroke, width = _STATUS_STROKE[status]
    dash = _STATUS_DASH.get(status)
    dashed = f",stroke-dasharray:{dash}" if dash else ""
    return f"    style {sid} stroke:{stroke},stroke-width:{width}{dashed}"
