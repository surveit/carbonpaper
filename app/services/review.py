"""Record a reviewer's verdict on one queued row as a stage-result cache entry
(app.core.stage_cache): enforce the one domain rule (a `modify` carries a
score), build the review stage's output row for the verdict, and write it.

The write goes through the read+write cache accessor
(`StageCacheEntry.read_write()`): recording a decision is the
one sanctioned way run activity persists something that outlives the run."""
from __future__ import annotations

from collections.abc import Mapping

from app.core.errors import ReviewValidationError
from app.models import RowReviewDecision
from app.core.stage_cache import StageCacheEntry


def record_decision(
    *, project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> None:
    """Build the output row one reviewed row produces and record it through the
    read+write cache accessor, passing the raw frozen row and the resolved
    fingerprints. A `modify` verdict must carry a `modified_score` (the one
    domain rule), else ReviewValidationError."""
    if verdict == RowReviewDecision.modify and modified_score is None:
        raise ReviewValidationError("modify requires modified_score")
    output_row = _build_output_row(frozen_row, verdict, modified_score, reviewer, reviewed_at)
    StageCacheEntry.read_write().record(
        project=project, stage_id=stage_id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        input_row=frozen_row, output_row=output_row,
    )


def _build_output_row(
    frozen_row: Mapping[str, object], verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> Mapping[str, object]:
    """The review stage's output row for one reviewed input: the frozen input
    plus the score columns the verdict produces. Every verdict produces a row —
    `approve` keeps the AI score as final, `modify` substitutes the
    human-entered score, and `reject` leaves the human and final scores null,
    carrying the verdict itself as what the reviewer decided."""
    ai = frozen_row.get("score")
    human = _resolve_human_score(verdict, ai, modified_score)
    return {
        **frozen_row,
        "ai_score": ai, "human_score": human, "final_score": human,
        "review_notes": f"decision={verdict.value}",
        "reviewer_id": reviewer, "reviewed_at": reviewed_at,
        "decision": verdict.value,
    }


def _resolve_human_score(
    verdict: RowReviewDecision, ai_score: object, modified_score: float | None
) -> object:
    """The score the reviewer's verdict settles on: the human-entered score for
    a `modify`, the AI score for an `approve`, and None for a `reject` — a
    rejected row has no score anyone stands behind, and a null says so where a
    kept AI score would read as an endorsement."""
    if verdict == RowReviewDecision.reject:
        return None
    if verdict == RowReviewDecision.modify:
        return modified_score
    return ai_score
