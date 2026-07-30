"""Shared helpers for per-stage-type column validation, on both the input and
output side.

`Stage` is imported only under `TYPE_CHECKING`: `app.models.stage` imports this
package back for its own model validator, so a runtime import would be circular."""
from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage import Stage

COLUMN_ISSUE = (
    "stage '{sid}': {field} references column '{col}' not in its input schema (declares {cols})"
)


def resolve_input_columns(stage: "Stage", index: int) -> set[str]:
    """The column names declared on `stage`'s input edge at `index` —
    `inputs[index].table_schema` (aliased `schema:` on a compiled stage).
    Deliberately EDGE-ONLY: a per-stage check must not reach for the
    upstream producer's own output_schema — this runs on one `Stage` in
    isolation, at construction time, so the producer may not even be present
    in whatever list of stages the caller happens to hold."""
    return {c.name for c in stage.inputs[index].table_schema.columns}


# A leading underscore marks the MACHINERY's namespace, never a real column. It
# is the STAGE contract that knows this, because it is the stage the runtime
# executes: the row driver attaches its internal per-row columns there (`_error`,
# `_usage`, `_deferred` — app/runtime/stages/execution.py) and strips them off
# every mapped frame; row provenance rides `_trace_source_stage`/
# `_trace_source_row` (app/runtime/lineage.py); and a stored stage's bookkeeping
# keys (`_filename`, `_order`, `_error`) are stripped BY PREFIX before the stage
# is compared or validated (app/services/{data_model,node_review}.py). We spend
# that namespace liberally, so a declared column inside it would be
# indistinguishable from machinery — silently stripped, summed as usage, or read
# back as lineage. A plain TableSchema knows nothing of this and does not need
# to: the ban is bought here, where the stage's schemas meet the runtime.
INTERNAL_COLUMN_PREFIX = "_"

INTERNAL_NAMESPACE_ISSUE = (
    "a column name may not begin with `{prefix}` — that namespace is reserved for "
    "the runtime's internal per-row columns"
)


def find_internal_namespace_column_issues(stage: "Stage") -> list[str]:
    """Every column `stage` declares — on its output_schema or on any input edge
    — that sits in the INTERNAL_COLUMN_PREFIX namespace; [] when none do, the
    only valid answer for a stored stage. Both sides are reported: an input edge
    is this stage's own declaration of what it requires, and an edge naming an
    internal column would claim the machinery is data. Top-level columns only —
    a `_`-prefixed key nested inside a `json` column is a value in that object,
    not a column on the frame, so it collides with nothing."""
    issues = [
        f"input `{ref.id}` declares column {name!r}"
        for ref in stage.inputs
        for name in _internal_namespace_columns(ref.table_schema)
    ]
    if stage.output_schema is not None:
        issues.extend(
            f"output_schema declares column {name!r}"
            for name in _internal_namespace_columns(stage.output_schema)
        )
    if issues:
        issues.append(INTERNAL_NAMESPACE_ISSUE.format(prefix=INTERNAL_COLUMN_PREFIX))
    return issues


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
    "stage '{sid}': output_schema declares column '{col}' that the {handle} handle "
    "cannot produce (producible columns: {cols})"
)
OUTPUT_TYPE_ISSUE = (
    "stage '{sid}': output_schema declares column '{col}' as {declared!r} but the "
    "{handle} handle produces {derived!r}"
)


def find_declared_vs_derived_issues(
    stage_id: str, handle_word: str, declared: "TableSchema", derived: Mapping[str, str | None]
) -> list[str]:
    """Issues for a declared output schema against the columns a handle can
    actually produce: `derived` maps each producible column name to its derived
    type, or None where the type is unknowable (e.g. a sum over a value column
    the edge schema does not name). Every declared column must be producible by
    name; where the derived
    type is known, the declared `type` must equal it. Nullability/enum/range are
    deliberately NOT compared — they are claims about data, not about what the
    handle can produce."""
    issues: list[str] = []
    for column in declared.columns:
        if column.name not in derived:
            issues.append(OUTPUT_UNPRODUCIBLE_ISSUE.format(
                sid=stage_id, col=column.name, handle=handle_word, cols=sorted(derived),
            ))
            continue
        derived_type = derived[column.name]
        if derived_type is not None and column.type != derived_type:
            issues.append(OUTPUT_TYPE_ISSUE.format(
                sid=stage_id, col=column.name, handle=handle_word,
                declared=column.type, derived=derived_type,
            ))
    return issues
