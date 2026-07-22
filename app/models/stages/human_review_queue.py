"""Config-column validation for a human_review_queue stage: every column its
`queue.filter` predicate references must resolve against the stage's input
edge."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.stages.shared import find_predicate_column_issues, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


def find_queue_filter_column_issues(stage: "Stage") -> list[str]:
    """Every column `queue.filter` references that is absent from the
    resolved single input — the check that catches a filter reading a column
    the review is meant to SET (e.g. a human-decision column that only exists
    after review), not one already produced upstream. [] when there is no
    filter, or the input's edge declares no schema at all."""
    queue = stage.queue
    assert queue is not None  # Stage._handle_for_type guarantees this for type="human_review_queue"
    if not queue.filter:
        return []
    cols = resolve_input_columns(stage, 0)
    if cols is None:
        return []
    return find_predicate_column_issues(queue.filter, stage_id=stage.id, field="queue.filter", cols=cols)
