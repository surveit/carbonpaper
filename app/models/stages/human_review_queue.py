"""human_review_queue stage: the config block, the reviewer's verdict
vocabulary, and config-column validation — every column the `queue.filter`
predicate references must resolve against the stage's input edge."""
from __future__ import annotations

from enum import Enum
from typing import Any, ClassVar, Literal, Optional

from pydantic import Field

from app.models.schema import StageConfig
from app.models.stage_base import StageBase, StageInput, StageType
from app.models.stages.shared import find_predicate_column_issues, resolve_input_columns
from app.models.stages.signature import ExtendsSignature

# The columns the review runtime itself writes onto every row — the only names a
# human_review_queue signature may add. Named here so the signature check and the
# authoring prompt (HUMAN_REVIEW_QUEUE_CONTRACT_NOTE) cannot drift apart.
REVIEW_OUTPUT_COLUMNS: frozenset[str] = frozenset({
    "decision", "ai_score", "human_score", "final_score",
    "review_notes", "reviewer_id", "reviewed_at",
})


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


class QueueConfig(StageConfig):
    """human_review_queue config block. A queued row is matched to a cached human
    decision by fingerprinting the row itself (app.core.stage_cache) — no
    column configuration is needed to enable that matching."""
    # `filter`/`reviewer_instructions` change what the human is asked; routing,
    # conflict_resolution, and estimated_volume_per_week describe how a
    # decision is routed, not what is asked — see
    # StageBase.compute_definition_fingerprint.
    FINGERPRINT_FIELDS: ClassVar[frozenset[str]] = frozenset({"filter", "reviewer_instructions"})
    INCIDENTAL_FIELDS: ClassVar[frozenset[str]] = frozenset({
        "routing", "conflict_resolution", "estimated_volume_per_week",
    })

    filter: Optional[str] = None
    reviewer_instructions: Optional[str] = None
    routing: Optional[str] = None
    conflict_resolution: Optional[str] = None
    estimated_volume_per_week: Optional[int] = None


class HumanReviewQueueStage(StageBase):
    type: Literal[StageType.human_review_queue]
    queue: QueueConfig
    inputs: list[StageInput] = Field(default_factory=list, min_length=1)
    signature: Optional[ExtendsSignature] = None

    def fingerprint_blocks(self) -> dict[str, StageConfig]:
        return {"queue": self.queue}

    def find_config_column_issues(self) -> list[str]:
        return find_queue_filter_column_issues(self)

    def find_signature_config_issues(self) -> list[str]:
        signature = self.signature
        assert signature is not None  # find_signature_config_issues runs only with one
        issues = [
            f"stage '{self.id}': signature adds `{column.name}`, which the review "
            f"runtime never writes — the only review columns are "
            f"{sorted(REVIEW_OUTPUT_COLUMNS)}"
            for column in signature.adds
            if column.name not in REVIEW_OUTPUT_COLUMNS
        ]
        if signature.rewrites:
            issues.append(
                f"stage '{self.id}': human_review_queue never revises an input "
                f"column; rewrites are not supported"
            )
        return issues


def find_queue_filter_column_issues(stage: "HumanReviewQueueStage") -> list[str]:
    """Every column `queue.filter` references that is absent from the
    resolved single input — the check that catches a filter reading a column
    the review is meant to SET (e.g. a human-decision column that only exists
    after review), not one already produced upstream. [] when there is no
    filter."""
    queue = stage.queue
    if not queue.filter:
        return []
    cols = resolve_input_columns(stage, 0)
    return find_predicate_column_issues(queue.filter, stage_id=stage.id, field="queue.filter", cols=cols)

# Authoring notes for this module's stage type(s), as the plain-data shape the
# authoring prompts render. Assembled into NODE_TYPES by app.models.stages.
NODE_TYPE_SPECS: dict[str, dict[str, Any]] = {
    "human_review_queue": {
        "summary": "Pulls flagged rows for human decision; halts the run.",
        "blocks": ["queue"],
        "requires_inputs": True,
        "min_inputs": 1,
        "required": [],
        "optional": ["filter", "reviewer_instructions",
                     "routing", "conflict_resolution", "estimated_volume_per_week"],
        "notes": (
            "Reviewed rows are matched to a cached human decision by "
            "fingerprinting the row itself — no column configuration is needed. "
            "Editing `filter` or `reviewer_instructions` changes the stage's "
            "definition fingerprint, so every previously cached decision for "
            "this stage stops matching and every row is asked again."
        ),
    },
}
