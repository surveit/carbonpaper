"""Record a reviewer's verdict on one queued row as a stage-result cache entry
(app.services.stage_cache): enforce the one domain rule (a `modify` carries a
score), build the review stage's output row for the verdict, and write it.

The write goes through the production cache accessor
(`StageCacheEntry.for_mode(RunMode.PRODUCTION)`): recording a decision is the
one sanctioned way run activity persists something that outlives the run."""
from __future__ import annotations

from collections.abc import Mapping

from app.core.errors import ReviewValidationError
from app.core.run_status import RunMode
from app.models import RowReviewDecision
from app.services.stage_cache import StageCacheEntry


def record_decision(
    *, project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> None:
    """Build the output row one reviewed row produces and record it through the
    production cache accessor, passing the raw frozen row and the resolved
    fingerprints. A `modify` verdict must carry a `modified_score` (the one
    domain rule), else ReviewValidationError."""
    if verdict == RowReviewDecision.modify and modified_score is None:
        raise ReviewValidationError("modify requires modified_score")
    output_row = _build_output_row(frozen_row, verdict, modified_score, reviewer, reviewed_at)
    StageCacheEntry.for_mode(RunMode.PRODUCTION).record(
        project=project, stage_id=stage_id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        input_row=frozen_row, output_row=output_row,
    )


def _build_output_row(
    frozen_row: Mapping[str, object], verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> Mapping[str, object] | None:
    """The review stage's output row for one reviewed input: the frozen input
    plus the score columns the verdict produces. A `reject` drops the row, so
    its output is None (a tombstone); `approve` keeps the AI score as final,
    `modify` substitutes the human-entered score."""
    if verdict == RowReviewDecision.reject:
        return None
    ai = frozen_row.get("score")
    human = modified_score if verdict == RowReviewDecision.modify else ai
    return {
        **frozen_row,
        "ai_score": ai, "human_score": human, "final_score": human,
        "review_notes": f"decision={verdict.value}",
        "reviewer_id": reviewer, "reviewed_at": reviewed_at,
        "decision": verdict.value,
    }
