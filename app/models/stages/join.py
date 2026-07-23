"""Column validation for a join stage, on both the input and output side:
every join key's `.left`/`.right` must resolve against its side's stage input
edge; and a declared output_schema (plus `select`) must be deliverable by the
columns the merge actually produces."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.schema import Column
from app.models.stages.shared import (
    COLUMN_ISSUE,
    find_declared_vs_derived_issues,
    resolve_input_columns,
)

if TYPE_CHECKING:
    from app.models.schema import TableSchema
    from app.models.stage import JoinConfig, Stage

SELECT_UNPRODUCIBLE_ISSUE = (
    "stage '{sid}': join.select references column '{col}' that the merge "
    "cannot produce (producible columns: {cols})"
)

# join.type values under which a carried column's own side can go unmatched
# (so pandas merge fills it with nulls) — compared only via `in` against this
# tuple, never as bare literals, so a new join.type value can't drift between
# two separately-typed comparisons (see tests/arch/test_repeated_string_literals.py).
_JOIN_TYPES_WHERE_RIGHT_OPTIONAL = ("left", "outer")
_JOIN_TYPES_WHERE_LEFT_OPTIONAL = ("right", "outer")


def find_join_column_issues(stage: "Stage") -> list[str]:
    """Every join key whose `.left`/`.right` names a column absent from its
    resolved side's input; a side whose edge declares no schema is skipped,
    not flagged."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    left = resolve_input_columns(stage, 0)
    right = resolve_input_columns(stage, 1)
    issues: list[str] = []
    for key in join.keys or join.on or []:
        if left is not None and key.left not in left:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .left", col=key.left, cols=sorted(left))
            )
        if right is not None and key.right not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .right", col=key.right, cols=sorted(right))
            )
    return issues


def find_join_output_issues(stage: "Stage") -> list[str]:
    """Every declared output_schema column (and select entry) the join handle
    cannot deliver. [] when the stage declares no output_schema and no select
    problem exists, or when either input edge declares no schema at all — the
    merged column set is then unknowable, never wrong."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    left = stage.inputs[0].table_schema
    right = stage.inputs[1].table_schema
    if left is None or right is None:
        return []
    merged = derive_join_output_types(join, left, right)
    issues = [
        SELECT_UNPRODUCIBLE_ISSUE.format(sid=stage.id, col=entry, cols=sorted(merged))
        for entry in join.select or []
        if entry not in merged
    ]
    if stage.output_schema is None:
        return issues
    effective = (
        {name: merged[name] for name in join.select if name in merged}
        if join.select else merged
    )
    issues.extend(
        find_declared_vs_derived_issues(stage.id, "join", stage.output_schema, effective)
    )
    return issues


def _carry_column(
    source: Column, *, new_name: str | None = None, force_nullable: bool
) -> Column:
    """One merged output column's `Column` spec: `source`'s own spec, renamed
    to `new_name` when it differs from `source.name`, and forced
    `nullable=True` when `force_nullable` (its side can be unmatched under the
    join's type, per the nullability policy)."""
    update: dict[str, object] = {}
    if new_name is not None and new_name != source.name:
        update["name"] = new_name
    if force_nullable:
        update["nullable"] = True
    return source.model_copy(update=update) if update else source.model_copy()


def fill_join_output_columns(stage: "Stage") -> list[Column] | None:
    """The fully-specified output columns this join stage's own handle would
    emit, for the auto-fill seam (app.models.stages.fill_output_schema) to use
    when `stage` declares no output_schema at all. Thin adapter over
    `derive_join_output_columns`: unwraps `stage.join` and both inputs' edge
    schemas, the same way `find_join_output_issues` does for the
    check-a-declared-schema side. None unless every output column is
    derivable."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    return derive_join_output_columns(join, stage.inputs[0].table_schema, stage.inputs[1].table_schema)


def derive_join_output_columns(
    join: "JoinConfig", left: "TableSchema | None", right: "TableSchema | None"
) -> list[Column] | None:
    """The fully-specified output columns the join handle's merge — then
    `select`, when declared — emits, or None unless every one is derivable.
    Mirrors pandas merge(..., suffixes=("", "_r")): a same-name key pair
    collapses into one column carrying the LEFT side's edge `Column` verbatim
    (never forced nullable — the merge always populates it from whichever
    side matched); every other column carries its source side's edge `Column`
    (renamed `<name>_r` on a right/left name collision), forced
    `nullable=True` when its own side can go unmatched under `join.type`
    (right columns under left/outer, left columns under right/outer). None
    when either edge is absent, or `select` names a column the merge cannot
    produce."""
    if left is None or right is None:
        return None
    keys = join.keys or join.on or []
    collapsed_key_names = {k.left for k in keys if k.left == k.right}
    right_optional = join.type in _JOIN_TYPES_WHERE_RIGHT_OPTIONAL
    left_optional = join.type in _JOIN_TYPES_WHERE_LEFT_OPTIONAL

    columns: list[Column] = []
    left_names = {c.name for c in left.columns}
    for column in left.columns:
        is_key = column.name in collapsed_key_names
        columns.append(_carry_column(column, force_nullable=left_optional and not is_key))
    for column in right.columns:
        if column.name in collapsed_key_names:
            continue
        new_name = column.name if column.name not in left_names else f"{column.name}_r"
        columns.append(_carry_column(column, new_name=new_name, force_nullable=right_optional))

    if not join.select:
        return columns
    by_name = {c.name: c for c in columns}
    selected: list[Column] = []
    for entry in join.select:
        if entry not in by_name:
            return None
        selected.append(by_name[entry])
    return selected


def derive_join_output_types(
    join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> dict[str, str]:
    """The columns the join handle's merge emits, each mapped to its type.
    Wraps `derive_join_output_columns`: with both edges given, that call
    fails only when `select` names an unproducible column — in which case
    this falls back to `_partial_join_output_types`'s full, unselected merge
    so the caller can still report which `select` entry is at fault; when
    `select` is absent or every entry is producible, the wrapped call's own
    columns (already `select`-projected, if declared) are the answer."""
    columns = derive_join_output_columns(join, left, right)
    if columns is not None:
        return {c.name: c.type for c in columns}
    return _partial_join_output_types(join, left, right)


def _partial_join_output_types(
    join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> dict[str, str]:
    """The columns the join handle's merge emits, each mapped to its type —
    mirroring pandas merge(..., suffixes=("", "_r")): all left columns keep
    their names and types; a right key whose pair shares the left key's name
    collapses into that left column; every other right column keeps its name
    unless it collides with a left column, in which case it appears as
    <name>_r. `select` projection is NOT applied here — the caller decides."""
    keys = join.keys or join.on or []
    collapsed_right_keys = {k.right for k in keys if k.left == k.right}
    merged: dict[str, str] = {c.name: c.type for c in left.columns}
    left_names = set(merged)
    for column in right.columns:
        if column.name in collapsed_right_keys:
            continue
        name = column.name if column.name not in left_names else f"{column.name}_r"
        merged[name] = column.type
    return merged
