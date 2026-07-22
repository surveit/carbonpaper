"""Shared helpers for per-stage-type config-column validation: resolving the
column names a stage's input edge declares, and turning a resolved check —
direct or via a where/filter predicate — into a human-readable issue string.

`from __future__ import annotations` plus `TYPE_CHECKING` below: `Stage` is
needed only for a type hint (attribute access on it needs no import at all),
never at runtime, since `app.models.stage` imports this package back for
its own model validator."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate

if TYPE_CHECKING:
    from app.models.stage import Stage

COLUMN_ISSUE = (
    "stage '{sid}': {field} references column '{col}' not in its input schema (declares {cols})"
)


def resolve_input_columns(stage: "Stage", index: int) -> set[str] | None:
    """The column names declared on `stage`'s input edge at `index` —
    `inputs[index].table_schema` (aliased `schema:` on a compiled stage) — or
    None when that edge declares no schema at all ("unknowable", never
    "empty"). Deliberately EDGE-ONLY: a per-stage check must not reach for the
    upstream producer's own output_schema — this runs on one `Stage` in
    isolation, at construction time, so the producer may not even be present
    in whatever list of stages the caller happens to hold."""
    schema = stage.inputs[index].table_schema
    return {c.name for c in schema.columns} if schema is not None else None


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
