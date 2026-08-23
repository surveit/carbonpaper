"""docs/no-silent-pick.md"""
from __future__ import annotations

from typing import Any, Sequence

from app.models.errors import StepRefused

DISAGREEMENT = (
    "stage '{sid}': {subject} — {n} different values of `{column}`: {values}. "
    "Collapsing them publishes one and drops the rest with nothing recording that the "
    "sources disagreed: settle it upstream, or carry the column with formula `list`."
)


def take_the_agreed_value(
    present: Sequence[Any], *, stage_id: str, column: str, subject: str
) -> Any:
    """`present` is null-free already: absence is not disagreement."""
    distinct = _distinct(present)
    if len(distinct) > 1:
        raise StepRefused(_describe_disagreement(stage_id, subject, column, distinct))
    return distinct[0] if distinct else None


def refuse_a_disagreeing_group(
    rows: Sequence[Sequence[Any]], columns: Sequence[str], *, stage_id: str, subject: str
) -> None:
    """Null counts as a value here: a survivor IS one of these rows, so a null is still a choice."""
    for position, column in enumerate(columns):
        distinct = _distinct([row[position] for row in rows])
        if len(distinct) > 1:
            raise StepRefused(_describe_disagreement(stage_id, subject, column, distinct))


def _describe_disagreement(
    stage_id: str, subject: str, column: str, values: Sequence[Any]
) -> str:
    shown = ", ".join(repr(value) for value in values[:4])
    if len(values) > 4:
        shown += f", and {len(values) - 4} more"
    return DISAGREEMENT.format(
        sid=stage_id, subject=subject, n=len(values), column=column, values=shown)


def _distinct(values: Sequence[Any]) -> list[Any]:
    seen: list[Any] = []
    for value in values:
        if not any(_same(value, kept) for kept in seen):
            seen.append(value)
    return seen


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, (list, tuple, dict, set)) or isinstance(right, (list, tuple, dict, set)):
        # No scalar equality on a container cell; compare what either one renders as.
        return str(left) == str(right)
    return bool(left == right)
