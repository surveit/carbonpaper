"""The signature a stored stage spec's `output_schema` and config imply.

Shared by alembic revision 0006 (the document store) and
tools.migrate_compiled_stage_files (a project's working copy), so a rewritten
store and a rewritten compiled file cannot disagree about what a spec meant.

REFUSES rather than guesses: an outer that dropped an anchor column does not
determine an `extends` signature, because every anchor column flows through one.
"""
from __future__ import annotations

from typing import Any

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate
from app.core.prompt_template import find_template_fields
from app.models.stages.stage_base import StageType

_EXTENDS_TYPES = frozenset({
    "llm_transform", "python_row_function", "starlark_row_function",
    "filter_rows", "human_review_queue", "enrich", "expand",
})
_REPLACES_TYPES = frozenset({
    "python_frame_function", "aggregate", "union", "input_data", "publish",
})


class SignatureUndeterminable(ValueError):
    """A stored stage spec whose signature cannot be read off what it stored."""


def add_signature(spec: dict[str, Any]) -> bool:
    """Give `spec` the signature its outer implies and drop the outer; False if unchanged."""
    if "signature" in spec and "output_schema" not in spec:
        return False
    if "signature" not in spec:
        signature = _synthesize_signature(spec)
        if signature is not None:
            spec["signature"] = signature
    return spec.pop("output_schema", _MISSING) is not _MISSING or "signature" in spec


_MISSING = object()


# ── v3: the signature the stored outer and config imply ──────────────────────
def _synthesize_signature(spec: dict[str, Any]) -> dict[str, Any] | None:
    stage_type = spec.get("type")
    if stage_type in _EXTENDS_TYPES:
        return _synthesize_extends(spec, stage_type)
    if stage_type in _REPLACES_TYPES:
        return _synthesize_replaces(spec, stage_type)
    return None


def _synthesize_extends(spec: dict[str, Any], stage_type: str) -> dict[str, Any] | None:
    """An anchored type: what the outer added to (or revised on) its first input."""
    edges = _edges(spec)
    if not edges:
        return None
    if stage_type in ("enrich", "expand"):
        return _synthesize_join(spec, edges)
    anchor_id, anchor_columns = edges[0]
    adds, rewrites = _split_outer_against_anchor(spec, anchor_columns)
    if stage_type == "filter_rows":
        return {"form": "extends"}  # keeps every kept row's columns unchanged
    if stage_type == "human_review_queue":
        return {"form": "extends", "adds": adds}
    if stage_type == "llm_transform":
        injected = _template_fields(spec)
        reads = [c for c in anchor_columns if c.get("name") in injected]
        return {"form": "extends", "reads": _reads(anchor_id, reads), "adds": adds}
    # Opaque code (python/starlark row functions) may consume anything, so the
    # whole anchor edge is the only honest read set.
    return {
        "form": "extends",
        "reads": _reads(anchor_id, anchor_columns),
        "adds": adds,
        "rewrites": rewrites,
    }


def _split_outer_against_anchor(
    spec: dict[str, Any], anchor_columns: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The outer's columns as (adds, rewrites) against the anchor edge.

    Refuses an outer that DROPPED an anchor column: an extends signature cannot
    express a drop — every anchor column flows — so the stored payload does not
    determine one.
    """
    outer = _columns(spec.get("output_schema"))
    anchor_by_name = {c.get("name"): c for c in anchor_columns}
    outer_names = {c.get("name") for c in outer}
    dropped = sorted(str(name) for name in anchor_by_name if name not in outer_names)
    if dropped:
        raise SignatureUndeterminable(
            f"stage {spec.get('id')!r} ({spec.get('type')}): its stored output_schema "
            f"drops input column(s) {dropped}, which an `extends` signature cannot "
            f"express — every anchor column flows. Author this stage's signature by "
            f"hand, or change it to a reshaping type."
        )
    adds = [c for c in outer if c.get("name") not in anchor_by_name]
    rewrites = [
        c for c in outer
        if c.get("name") in anchor_by_name and c != anchor_by_name[c.get("name")]
    ]
    return adds, rewrites


def _synthesize_join(
    spec: dict[str, Any], edges: list[tuple[Any, list[dict[str, Any]]]]
) -> dict[str, Any] | None:
    """A join reads its keys from each side and adds exactly what `enrich_with` lands."""
    if len(edges) < 2:
        return None
    join = spec.get("join") or {}
    (subject_id, subject_columns), (reference_id, reference_columns) = edges[0], edges[1]
    subject_by_name = {c.get("name"): c for c in subject_columns}
    reference_by_name = {c.get("name"): c for c in reference_columns}
    outer_by_name = {c.get("name"): c for c in _columns(spec.get("output_schema"))}

    adds = []
    for source, landed in (join.get("enrich_with") or {}).items():
        column = reference_by_name.get(source)
        if column is None:
            return None  # the config check reports the unresolvable source
        # An unmatched subject row lands null whatever the reference declared, so
        # the outer's own nullability is the one the downstream edges were built on.
        stored = outer_by_name.get(landed) or {}
        adds.append({**column, "name": landed,
                     "nullable": stored.get("nullable", True)})

    reads = (
        _reads(subject_id, [subject_by_name[k["left"]] for k in join.get("keys") or []
                            if k.get("left") in subject_by_name])
        + _reads(reference_id, [reference_by_name[k["right"]] for k in join.get("keys") or []
                                if k.get("right") in reference_by_name])
    )
    return {"form": "extends", "reads": reads, "adds": adds}


def _synthesize_replaces(spec: dict[str, Any], stage_type: str) -> dict[str, Any] | None:
    """A reshaping type: the outer IS `produces`; only the read set varies."""
    edges = _edges(spec)
    produces = _columns(spec.get("output_schema"))
    if stage_type == "publish":
        return {"form": "replaces", "reads": _all_edge_reads(edges)}
    if stage_type in ("union", "input_data"):
        # A union consumes no column; input_data has no input to read from.
        return {"form": "replaces", "produces": produces}
    if stage_type == "aggregate":
        if not edges:
            return None
        anchor_id, anchor_columns = edges[0]
        consumed = _aggregate_consumed(spec.get("aggregate") or {})
        reads = [c for c in anchor_columns if c.get("name") in consumed]
        return {"form": "replaces", "reads": _reads(anchor_id, reads), "produces": produces}
    # python_frame_function: opaque code over every input frame.
    return {"form": "replaces", "reads": _all_edge_reads(edges), "produces": produces}


def _aggregate_consumed(aggregate: dict[str, Any]) -> set[str]:
    consumed = set(aggregate.get("group_by") or [])
    for op in aggregate.get("aggregations") or []:
        if op.get("value_column"):
            consumed.add(op["value_column"])
        if op.get("where"):
            try:
                consumed.update(parse_predicate(op["where"]).columns)
            except PredicateError:
                pass  # the stage's own config check reports the bad predicate
    return consumed


def _template_fields(spec: dict[str, Any]) -> set[str]:
    llm = spec.get("llm") or {}
    template = llm.get("prompt_data_template") or llm.get("prompt_template") or ""
    return set(find_template_fields(template))


def _edges(spec: dict[str, Any]) -> list[tuple[Any, list[dict[str, Any]]]]:
    return [
        (ref.get("id"), _columns(ref.get("schema") or ref.get("table_schema")))
        for ref in spec.get("inputs") or []
        if isinstance(ref, dict)
    ]


def _columns(schema: Any) -> list[dict[str, Any]]:
    if isinstance(schema, dict):
        return [c for c in schema.get("columns") or [] if isinstance(c, dict)]
    return []


def _reads(input_id: Any, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"input": input_id, "columns": columns}] if columns else []


def _all_edge_reads(
    edges: list[tuple[Any, list[dict[str, Any]]]]
) -> list[dict[str, Any]]:
    return [entry for input_id, columns in edges for entry in _reads(input_id, columns)]
