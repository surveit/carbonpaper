"""Shared helpers for per-stage-type column validation, on both the input and
output side.

`StageBase` is imported only under `TYPE_CHECKING`: `app.models.stage_base`
imports this module at runtime, so importing it back would be circular."""
from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage_base import StageBase

COLUMN_ISSUE = (
    "stage '{sid}': {field} references column '{col}' not in its input schema (declares {cols})"
)


def resolve_input_columns(stage: "StageBase", index: int) -> set[str]:
    """The column names declared on `stage`'s input edge at `index` —
    `inputs[index].table_schema` (aliased `schema:` on a compiled stage).
    Deliberately EDGE-ONLY: a per-stage check must not reach for the
    upstream producer's own output schema — this runs on one `Stage` in
    isolation, at construction time, so the producer may not even be present
    in whatever list of stages the caller happens to hold."""
    return {c.name for c in stage.inputs[index].table_schema.columns}


# The runtime spends this prefix on machinery — internal per-row columns, row
# lineage, stored-stage bookkeeping — so a column declared inside it would be
# indistinguishable from any of that (tests/arch/test_internal_columns_are_prefixed.py
# holds the other end: every internal key stays under this prefix).
INTERNAL_COLUMN_PREFIX = "_"

INTERNAL_NAMESPACE_ISSUE = (
    "a column name may not begin with `{prefix}` — that namespace is reserved for "
    "the runtime's internal per-row columns"
)


def find_internal_namespace_column_issues(stage: "StageBase") -> list[str]:
    issues = [
        f"input `{ref.id}` declares column {name!r}"
        for ref in stage.inputs
        for name in _internal_namespace_columns(ref.table_schema)
    ]
    issues.extend(
        f"signature declares column {name!r}"
        for name in _signature_column_names(stage)
        if name.startswith(INTERNAL_COLUMN_PREFIX)
    )
    if issues:
        issues.append(INTERNAL_NAMESPACE_ISSUE.format(prefix=INTERNAL_COLUMN_PREFIX))
    return issues


def _signature_column_names(stage: "StageBase") -> list[str]:
    """Every column name the signature mentions, duck-typed over both forms; [] without one."""
    signature = stage.signature
    if signature is None:
        return []
    names = [
        column.name for entry in signature.reads for column in entry.columns
    ]
    for field in ("adds", "rewrites", "produces"):
        names.extend(column.name for column in getattr(signature, field, []))
    return names


def _internal_namespace_columns(schema: "TableSchema") -> list[str]:
    """`schema`'s own column names that sit in the reserved namespace."""
    return [c.name for c in schema.columns if c.name.startswith(INTERNAL_COLUMN_PREFIX)]


def find_predicate_column_issues(
    expr: str, *, stage_id: str, field: str, cols: set[str]
) -> list[str]:
    """Issues for one where/filter predicate `expr` against the resolved
    column set `cols`: a single issue naming the parse failure when `expr`
    falls outside `app.core.predicate.parse_predicate`'s grammar, else one
    `COLUMN_ISSUE` per column `expr` references that is absent from `cols`."""
    try:
        referenced = parse_predicate(expr).columns
    except PredicateError as exc:
        return [f"stage '{stage_id}': {field}: {exc}"]
    return [
        COLUMN_ISSUE.format(sid=stage_id, field=field, col=col, cols=sorted(cols))
        for col in sorted(referenced)
        if col not in cols
    ]


OUTPUT_UNPRODUCIBLE_ISSUE = (
    "stage '{sid}': {block} declares column '{col}' that the config "
    "cannot produce (producible columns: {cols})"
)
OUTPUT_TYPE_ISSUE = (
    "stage '{sid}': {block} declares column '{col}' as {declared!r} but the "
    "config produces {produced!r}"
)


def find_declared_vs_computed_issues(
    stage_id: str, block_name: str, declared: "TableSchema", computed: Mapping[str, str | None]
) -> list[str]:
    """Issues for a declared column set against the columns a config block can
    actually produce: `computed` maps each producible column name to its computed
    type, or None where the type is unknowable (e.g. a sum over a value column
    the edge schema does not name). Every declared column must be producible by
    name; where the computed
    type is known, the declared `type` must equal it. Nullability/enum/range are
    deliberately NOT compared — they are claims about data, not about what the
    config block can produce."""
    issues: list[str] = []
    for column in declared.columns:
        if column.name not in computed:
            issues.append(OUTPUT_UNPRODUCIBLE_ISSUE.format(
                sid=stage_id, col=column.name, block=block_name, cols=sorted(computed),
            ))
            continue
        computed_type = computed[column.name]
        if computed_type is not None and column.type != computed_type:
            issues.append(OUTPUT_TYPE_ISSUE.format(
                sid=stage_id, col=column.name, block=block_name,
                declared=column.type, produced=computed_type,
            ))
    return issues
