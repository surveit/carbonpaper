"""Config-column validation for a join stage: every join key's `.left`/
`.right` must resolve against its side's stage input edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


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
