"""Idempotent upgrades applied to STORED stage-spec payloads on every read, so
an old store loads instead of refusing; the store's schema_version records what
a payload was WRITTEN at. Authoring paths never upgrade — a NEW spec in an old
shape is refused loudly."""
from __future__ import annotations

from typing import Any

from app.core.predicate import PredicateError, parse_predicate
from app.core.prompt_template import find_template_fields
from app.models.stage_base import StageType

# v2: primary_key left the stage vocabulary (the data model keeps its own).
# v3: the stored outer left too — a spec carries a `signature`, synthesized
# here from the outer + config for payloads written before signatures, and
# the output schema resolves from it (app.models.stages.signature).
STAGE_SPEC_SCHEMA_VERSION = 3

_EXTENDS_TYPES = {StageType.llm_transform, StageType.python_row_function,
                  StageType.starlark_row_function, StageType.filter_rows,
                  StageType.human_review_queue, StageType.enrich, StageType.expand}
_REPLACES_TYPES = {StageType.python_frame_function, StageType.aggregate,
                   StageType.union, StageType.input_data, StageType.publish}


def upgrade_stage_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """`spec` upgraded in place to the current stage-spec shape, and returned."""
    for ref in spec.get("inputs") or []:
        if isinstance(ref, dict):
            _drop_primary_key(ref.get("schema"))
            _drop_primary_key(ref.get("table_schema"))
    _drop_primary_key(spec.get("output_schema"))
    if "signature" not in spec:
        signature = _synthesize_signature(spec)
        if signature is not None:
            spec["signature"] = signature
    spec.pop("output_schema", None)
    return spec


def _drop_primary_key(schema: Any) -> None:
    if isinstance(schema, dict):
        schema.pop("primary_key", None)


# ── v3 synthesis: the signature the stored outer and config imply ────────────


def _synthesize_signature(spec: dict[str, Any]) -> dict[str, Any] | None:
    stage_type = spec.get("type")
    if "output_schema" not in spec and stage_type != StageType.publish:
        # A pre-v3 spec always stored an outer (publish excepted); one missing
        # both was never valid, and synthesizing a contract for it would
        # fabricate one — leave it to be refused at parse.
        return None
    outer = _columns(spec.get("output_schema"))
    if stage_type in _EXTENDS_TYPES:
        return _synthesize_extends(spec, stage_type, outer)
    if stage_type in _REPLACES_TYPES:
        return _synthesize_replaces(spec, stage_type, outer)
    return None


def _synthesize_extends(
    spec: dict[str, Any], stage_type: Any, outer: list[dict[str, Any]]
) -> dict[str, Any] | None:
    edges = _edges(spec)
    if not edges:
        return None
    anchor_id, anchor_columns = edges[0]
    anchor_by_name = {c.get("name"): c for c in anchor_columns}
    adds = [c for c in outer if c.get("name") not in anchor_by_name]
    rewrites = [
        c for c in outer
        if c.get("name") in anchor_by_name and c != anchor_by_name[c.get("name")]
    ]
    if stage_type == StageType.filter_rows:
        # A filter keeps every kept row's columns unchanged: reads only.
        return {"form": "extends", "reads": [], "adds": [], "rewrites": []}
    if stage_type == StageType.human_review_queue:
        return {"form": "extends", "reads": [], "adds": adds, "rewrites": []}
    if stage_type == StageType.llm_transform:
        injected = _template_fields(spec)
        reads = [c for c in anchor_columns if c.get("name") in injected]
        return {"form": "extends",
                "reads": _reads_entry(anchor_id, reads), "adds": adds, "rewrites": []}
    if stage_type in (StageType.enrich, StageType.expand):
        return _synthesize_join(spec, edges)
    # Opaque code (python/starlark row functions) may consume anything: the
    # whole anchor edge is the honest read set.
    return {"form": "extends",
            "reads": _reads_entry(anchor_id, anchor_columns),
            "adds": adds, "rewrites": rewrites}


def _synthesize_join(
    spec: dict[str, Any], edges: list[tuple[Any, list[dict[str, Any]]]]
) -> dict[str, Any] | None:
    join = spec.get("join") or {}
    if len(edges) < 2:
        return None
    (subject_id, subject_columns), (reference_id, reference_columns) = edges[0], edges[1]
    subject_by_name = {c.get("name"): c for c in subject_columns}
    reference_by_name = {c.get("name"): c for c in reference_columns}
    left_keys = [k.get("left") for k in join.get("keys") or []]
    right_keys = [k.get("right") for k in join.get("keys") or []]
    adds = []
    for src, landed in (join.get("enrich_with") or {}).items():
        source = reference_by_name.get(src)
        if source is None:
            return None
        # Landed columns are null on an unmatched subject row, whatever the
        # source declared.
        adds.append({**source, "name": landed, "nullable": True})
    reads = (
        _reads_entry(subject_id, [subject_by_name[k] for k in left_keys if k in subject_by_name])
        + _reads_entry(reference_id, [reference_by_name[k] for k in right_keys if k in reference_by_name])
    )
    return {"form": "extends", "reads": reads, "adds": adds, "rewrites": []}


def _synthesize_replaces(
    spec: dict[str, Any], stage_type: Any, outer: list[dict[str, Any]]
) -> dict[str, Any] | None:
    edges = _edges(spec)
    if stage_type == StageType.publish:
        return {"form": "replaces", "reads": _all_edge_reads(edges), "produces": []}
    if stage_type == StageType.union:
        return {"form": "replaces", "reads": [], "produces": outer}
    if stage_type == StageType.input_data:
        return {"form": "replaces", "reads": [], "produces": outer}
    if stage_type == StageType.aggregate:
        if not edges:
            return None
        anchor_id, anchor_columns = edges[0]
        consumed = _aggregate_consumed(spec.get("aggregate") or {})
        reads = [c for c in anchor_columns if c.get("name") in consumed]
        return {"form": "replaces",
                "reads": _reads_entry(anchor_id, reads), "produces": outer}
    # python_frame_function: opaque code over every input frame.
    return {"form": "replaces", "reads": _all_edge_reads(edges), "produces": outer}


def _aggregate_consumed(aggregate: dict[str, Any]) -> set[str]:
    consumed = set(aggregate.get("group_by") or [])
    for op in aggregate.get("aggregations") or []:
        if op.get("value_column"):
            consumed.add(op["value_column"])
        if op.get("where"):
            try:
                consumed.update(parse_predicate(op["where"]).columns)
            except PredicateError:
                pass  # the config checks report the bad predicate
    return consumed


def _template_fields(spec: dict[str, Any]) -> set[str]:
    llm = spec.get("llm") or {}
    template = llm.get("prompt_data_template") or llm.get("prompt_template") or ""
    return set(find_template_fields(template))


def _edges(spec: dict[str, Any]) -> list[tuple[Any, list[dict[str, Any]]]]:
    edges: list[tuple[Any, list[dict[str, Any]]]] = []
    for ref in spec.get("inputs") or []:
        if not isinstance(ref, dict):
            continue
        schema = ref.get("schema") or ref.get("table_schema") or {}
        edges.append((ref.get("id"), _columns(schema)))
    return edges


def _columns(schema: Any) -> list[dict[str, Any]]:
    if isinstance(schema, dict):
        return [c for c in schema.get("columns") or [] if isinstance(c, dict)]
    return []


def _reads_entry(input_id: Any, columns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not columns:
        return []
    return [{"input": input_id, "columns": columns}]


def _all_edge_reads(edges: list[tuple[Any, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    reads: list[dict[str, Any]] = []
    for input_id, columns in edges:
        reads.extend(_reads_entry(input_id, columns))
    return reads
