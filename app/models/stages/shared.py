# `Stage` is imported only under TYPE_CHECKING: app.models.stage imports this package
# back for its own model validator, so a runtime import would be circular.
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
    # EDGE-ONLY, deliberately: read the input edge's own schema, never the upstream
    # producer's output_schema — this runs on one `Stage` in isolation at construction
    # time, so that producer may not even be present in the caller's list of stages.
    return {c.name for c in stage.inputs[index].table_schema.columns}


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
