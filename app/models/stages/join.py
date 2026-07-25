"""Column validation for a join stage, on both the input and output side:
every join key's `.left`/`.right` must resolve against its side's stage input
edge; and a declared output_schema (plus `select`) must be deliverable by the
columns the merge actually produces."""
from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

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
MERGE_COLLISION_ISSUE = (
    "stage '{sid}': the join cannot run — the merge would name two columns "
    "'{col}' ({sources}); rename one of them upstream"
)


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
    merged column set is then unknowable, never wrong.

    A merge whose suffixing would collide short-circuits: pandas rejects the
    merge itself, so no column is deliverable and every further complaint
    would be noise built on a merged column set that never materialises."""
    join = stage.join
    assert join is not None  # Stage._handle_for_type guarantees this for type="join"
    left = stage.inputs[0].table_schema
    right = stage.inputs[1].table_schema
    if left is None or right is None:
        return []
    collisions = find_join_merge_collisions(stage.id, join, left, right)
    if collisions:
        return collisions
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


class MergedColumn(NamedTuple):
    """One column the merge emits: `name` after suffixing, the `type` it carries
    from its own side, and `source` — the side plus pre-merge name it came from,
    which is what a collision message has to name to be actionable."""
    name: str
    type: str
    source: str


def derive_join_merge_columns(
    join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> list[MergedColumn]:
    """Every column the join handle's merge emits, in merge order and BEFORE any
    `select` projection — mirroring pandas merge(..., suffixes=("", "_r")): all
    left columns keep their names and types; a right key whose pair shares the
    left key's name collapses into that left column; every other right column
    keeps its name unless it collides with a left column, in which case it
    appears as <name>_r.

    One entry per SOURCE column, so two entries can share a `name` — exactly the
    case pandas refuses to merge at all (see `find_join_merge_collisions`), and
    the reason this is a list rather than a name-keyed mapping."""
    keys = join.keys or join.on or []
    collapsed_right_keys = {k.right for k in keys if k.left == k.right}
    left_names = {c.name for c in left.columns}
    merged = [MergedColumn(c.name, c.type, f"left column '{c.name}'") for c in left.columns]
    for column in right.columns:
        if column.name in collapsed_right_keys:
            continue
        name = column.name if column.name not in left_names else f"{column.name}_r"
        merged.append(MergedColumn(name, column.type, f"right column '{column.name}'"))
    return merged


def derive_join_output_types(
    join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> dict[str, str]:
    """`derive_join_merge_columns` keyed by merged column name. Only meaningful
    for a merge that can actually run: when two source columns claim one merged
    name this mapping silently keeps the last, so callers check
    `find_join_merge_collisions` FIRST and never reach here on a collision."""
    return {c.name: c.type for c in derive_join_merge_columns(join, left, right)}


def find_join_merge_collisions(
    stage_id: str, join: "JoinConfig", left: "TableSchema", right: "TableSchema"
) -> list[str]:
    """One issue per merged column name that two source columns would both
    produce — e.g. left `x` + left `x_r` against right `x`, where the right
    column's suffixed name is already taken. pandas raises MergeError
    ("Passing 'suffixes' which cause duplicate columns") for exactly these, so
    the stage produces NO output at all rather than a wrong one; saying so at
    save time beats discovering it mid-run."""
    sources_by_name: dict[str, list[str]] = {}
    for column in derive_join_merge_columns(join, left, right):
        sources_by_name.setdefault(column.name, []).append(column.source)
    return [
        MERGE_COLLISION_ISSUE.format(sid=stage_id, col=name, sources=" and ".join(sources))
        for name, sources in sorted(sources_by_name.items())
        if len(sources) > 1
    ]
