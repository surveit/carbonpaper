"""Config-column validation for a publish stage: `one_file_per`, when set,
must resolve against the stage's input edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.stages.shared import COLUMN_ISSUE, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


def find_publish_column_issues(stage: "Stage") -> list[str]:
    """One issue if `publish.one_file_per` is set and absent from the
    resolved single input; [] when unset, valid, or the input's edge declares
    no schema at all."""
    publish = stage.publish
    assert publish is not None  # Stage._handle_for_type guarantees this for type="publish"
    if not publish.one_file_per:
        return []
    cols = resolve_input_columns(stage, 0)
    if cols is None or publish.one_file_per in cols:
        return []
    return [
        COLUMN_ISSUE.format(
            sid=stage.id, field="publish.one_file_per", col=publish.one_file_per, cols=sorted(cols)
        )
    ]
