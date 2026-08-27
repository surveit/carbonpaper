"""The signature a stored stage spec's `output_schema` and config imply.

Shared by alembic revision 0006 (the document store) and
the alembic revision that rewrites the stored payloads, so a rewritten
store and a rewritten compiled file cannot disagree about what a spec meant.

REFUSES rather than guesses: an outer that dropped an anchor column does not
determine an `extends` signature, because every anchor column flows through one.
"""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError as PydanticValidationError

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate
from app.core.prompt_template import find_template_fields
from app.models.stages.human_review_queue import QueueConfig, find_added_columns
from app.models.stages.stage_base import StageType

_EXTENDS_TYPES = frozenset({
    "llm_transform", "python_row_function", "starlark_row_function",
    "filter_rows", "human_review_queue", "enrich", "expand",
})
_REPLACES_TYPES = frozenset({
    "python_frame_function", "aggregate", "union", "input_data",
    # Both spellings: 0006 reads what the store held, and 0017 renamed publish to report.
    "publish", "report",
})
# The two types whose model REFUSES an empty read set, so a spec carrying one
# does not load at all (app.models.stages.{filter_rows,human_review_queue}).
_READS_THE_WHOLE_ANCHOR = frozenset({"filter_rows", "human_review_queue"})


class SignatureUndeterminable(ValueError):
    """A stored stage spec whose signature cannot be read off what it stored."""


def add_signature(spec: dict[str, Any], *, allow_drops: bool = False) -> bool:
    """Give `spec` the signature its outer implies and drop the outer; False if unchanged."""
    # allow_drops=True turns the refusal below into a widening: the dropped
    # columns FLOW into this stage's output, so the migrated stage emits columns
    # the stored spec said it did not. Callers report what they widened —
    # find_dropped_anchor_columns names them before the signature is written.
    if "signature" in spec and "output_schema" not in spec:
        return False
    if "signature" not in spec:
        signature = _synthesize_signature(spec, allow_drops)
        if signature is not None:
            spec["signature"] = signature
    return spec.pop("output_schema", _MISSING) is not _MISSING or "signature" in spec


def backfill_anchor_reads(spec: dict[str, Any]) -> bool:
    """Give a filter_rows/queue spec whose `reads` are EMPTY the whole anchor edge;
    False if unchanged."""
    # Both types run an authored expression over the row — a predicate, a queue
    # `filter` — so the whole anchor edge is what they may consume, and an empty
    # `reads` understates it. A stage that already names columns was authored or
    # repaired deliberately and is never widened here; one whose input declares no
    # columns is left alone rather than given an invented edge.
    if spec.get("type") not in _READS_THE_WHOLE_ANCHOR:
        return False
    signature = spec.get("signature")
    if not isinstance(signature, dict) or signature.get("reads"):
        return False
    edges = _edges(spec)
    if not edges:
        return False
    anchor_id, anchor_columns = edges[0]
    reads = _reads(anchor_id, anchor_columns)
    if not reads:
        return False
    signature["reads"] = reads
    return True


def find_dropped_anchor_columns(spec: dict[str, Any]) -> list[str]:
    """The anchor columns a stored outer dropped — what `extends` cannot express."""
    if spec.get("type") not in _EXTENDS_TYPES:
        return []
    # A join's adds come from `enrich_with`, not from diffing the outer, so it has
    # no anchor to drop from.
    if spec.get("type") in (StageType.enrich, StageType.expand):
        return []
    edges = _edges(spec)
    if not edges:
        return []
    _, anchor_columns = edges[0]
    outer_names = {c.get("name") for c in _columns(spec.get("output_schema"))}
    return sorted(str(c.get("name")) for c in anchor_columns
                  if c.get("name") not in outer_names)


_MISSING = object()


# ── v3: the signature the stored outer and config imply ──────────────────────
def _synthesize_signature(spec: dict[str, Any], allow_drops: bool) -> dict[str, Any] | None:
    stage_type = spec.get("type")
    if stage_type in _EXTENDS_TYPES:
        return _synthesize_extends(spec, stage_type, allow_drops)
    if stage_type in _REPLACES_TYPES:
        return _synthesize_replaces(spec, stage_type)
    return None


def _synthesize_extends(
    spec: dict[str, Any], stage_type: str, allow_drops: bool
) -> dict[str, Any] | None:
    """An anchored type: what the outer added to (or revised on) its first input."""
    edges = _edges(spec)
    if not edges:
        return None
    if stage_type in (StageType.enrich, StageType.expand):
        return _synthesize_join(spec, edges)
    anchor_id, anchor_columns = edges[0]
    adds, rewrites = _split_outer_against_anchor(spec, anchor_columns, allow_drops)
    # Both of these run an authored expression over the row — filter_rows its
    # predicate, a queue its `filter` — so like the opaque code below, the whole
    # anchor edge is the only honest read set.
    if stage_type == StageType.filter_rows:
        # No adds: it keeps every kept row's columns unchanged.
        return {"form": "extends", "reads": _reads(anchor_id, anchor_columns)}
    if stage_type == StageType.human_review_queue:
        return {"form": "extends", "reads": _reads(anchor_id, anchor_columns),
                "adds": _queue_adds(spec)}
    if stage_type == StageType.llm_transform:
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
    spec: dict[str, Any], anchor_columns: list[dict[str, Any]], allow_drops: bool = False
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """As (adds, rewrites) against the anchor edge; refuses an outer that DROPPED a column."""
    outer = _columns(spec.get("output_schema"))
    # A drop is inexpressible in `extends` — every anchor column flows — so the
    # payload does not determine a signature and a human must author it. Under
    # allow_drops the caller has decided the drop was not intended: the columns
    # flow and this stage's output widens by exactly them.
    anchor_by_name = {c.get("name"): c for c in anchor_columns}
    outer_names = {c.get("name") for c in outer}
    dropped = sorted(str(name) for name in anchor_by_name if name not in outer_names)
    if dropped and not allow_drops:
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


def _queue_adds(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """A queue stage's adds: the columns its queue block names, in that order."""
    # NOT the outer-minus-anchor diff every other anchored type uses. The model
    # ties the two accounts together — signature.adds and the queue block must
    # name the same columns — so a stored outer that also carried upstream
    # columns would synthesize adds the review runtime never writes.
    try:
        queue = QueueConfig.model_validate(spec.get("queue") or {})
    except PydanticValidationError as err:
        raise SignatureUndeterminable(
            f"stage {spec.get('id')!r} (human_review_queue): its queue block does "
            f"not read, so what the stage adds is unknown: {err}"
        ) from err
    outer_by_name = {c.get("name"): c for c in _columns(spec.get("output_schema"))}
    adds: list[dict[str, Any]] = []
    for _, name in find_added_columns(queue):
        column = outer_by_name.get(name)
        if column is not None and column not in adds:
            adds.append(column)
    return adds


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
    if stage_type == StageType.report:
        return {"form": "replaces", "reads": _all_edge_reads(edges)}
    if stage_type in (StageType.union, StageType.input_data):
        # A union consumes no column; input_data has no input to read from.
        return {"form": "replaces", "produces": produces}
    if stage_type == StageType.aggregate:
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
