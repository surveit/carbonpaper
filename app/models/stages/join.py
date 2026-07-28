"""Column validation for a join stage, on both the input and output side:
every join key's `.left`/`.right` must resolve against its side's stage input
edge; and a declared output_schema (plus `select`) must be deliverable by the
columns the merge actually produces."""
from __future__ import annotations

from typing import TYPE_CHECKING

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


def find_join_column_issues(stage: "Stage") -> list[str]:
    """Every join key whose `.left`/`.right` names a column absent from its
    resolved side's input."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    left = resolve_input_columns(stage, 0)
    right = resolve_input_columns(stage, 1)
    issues: list[str] = []
    for key in join.keys or join.on or []:
        if key.left not in left:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .left", col=key.left, cols=sorted(left))
            )
        if key.right not in right:
            issues.append(
                COLUMN_ISSUE.format(sid=stage.id, field="join key .right", col=key.right, cols=sorted(right))
            )
    return issues


def find_join_output_issues(stage: "Stage") -> list[str]:
    """Every declared output_schema column (and select entry) the join handle
    cannot deliver."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    assert stage.output_schema is not None  # Stage._schemas_declared guarantees this off publish
    left = stage.inputs[0].table_schema
    right = stage.inputs[1].table_schema
    merged = derive_join_output_types(join, left, right)
    issues = [
        SELECT_UNPRODUCIBLE_ISSUE.format(sid=stage.id, col=entry, cols=sorted(merged))
        for entry in join.select or []
        if entry not in merged
    ]
    effective = (
        {name: merged[name] for name in join.select if name in merged}
        if join.select else merged
    )
    issues.extend(
        find_declared_vs_derived_issues(stage.id, "join", stage.output_schema, effective)
    )
    return issues


def derive_join_output_types(
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
