"""human_review_queue stage: the queue handle, the reviewer's per-row verdict
vocabulary, and the config-column check that every column `queue.filter`
references resolves against the stage's input edge."""
from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, ClassVar, Optional

from app.models.schema import _Base
from app.models.stages.shared import find_predicate_column_issues, resolve_input_columns

if TYPE_CHECKING:
    from app.models.stage import Stage


class RowReviewDecision(str, Enum):
    """A reviewer's verdict on one human_review_queue row, validated and applied
    at the web/service boundary (app.services.review) and recorded as the review
    stage's output row in the cache: `approve` keeps the AI score as final,
    `modify` substitutes a human-entered score, `reject` leaves the human and
    final scores null. EVERY verdict produces an output row — the review stage
    emits one row per input row — so a rejected row reaches the stage's output
    carrying its rejection, and excluding it is a downstream stage's job."""
    approve = "approve"
    modify = "modify"
    reject = "reject"


class QueueConfig(_Base):
    """human_review_queue handle. A queued row is matched to a cached human
    decision by fingerprinting the row itself (app.core.stage_cache) — no
    column configuration is needed to enable that matching."""
    # `filter`/`reviewer_instructions` change what the human is asked; routing,
    # conflict_resolution, and estimated_volume_per_week describe how a
    # decision is routed, not what is asked — see
    # Stage.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"filter", "reviewer_instructions"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "routing", "conflict_resolution", "estimated_volume_per_week",
    })

    filter: Optional[str] = None
    reviewer_instructions: Optional[str] = None
    routing: Optional[str] = None
    conflict_resolution: Optional[str] = None
    estimated_volume_per_week: Optional[int] = None


def find_queue_filter_column_issues(stage: "Stage") -> list[str]:
    """Every column `queue.filter` references that is absent from the
    resolved single input — the check that catches a filter reading a column
    the review is meant to SET (e.g. a human-decision column that only exists
    after review), not one already produced upstream. [] when there is no
    filter."""
    queue = stage.queue
    assert queue is not None  # Stage._handle_for_type guarantees this for type="human_review_queue"
    if not queue.filter:
        return []
    cols = resolve_input_columns(stage, 0)
    return find_predicate_column_issues(queue.filter, stage_id=stage.id, field="queue.filter", cols=cols)
