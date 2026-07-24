"""Record a reviewer's verdict on one queued row as a stage-result cache entry
(app.services.stage_cache): enforce the one domain rule (a `modify` carries a
score), build the review stage's output row for the verdict, and write it.

The write goes through the production cache accessor
(`StageCacheEntry.for_mode(RunMode.PRODUCTION)`): recording a decision is the
one sanctioned way run activity persists something that outlives the run."""
from __future__ import annotations

from collections.abc import Mapping

from app.core.errors import ReviewValidationError
from app.core.persistence import JsonDict
from app.core.run_status import RunMode
from app.models import RowReviewDecision
from app.services.stage_cache import (
    StageCacheEntry,
    build_cache_id,
    to_json_safe_row,
)


def record_decision(
    *, project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> None:
    """Build the cache entry for one reviewed row and write it through the
    production cache accessor. A `modify` verdict must carry a `modified_score`
    (the one domain rule), else ReviewValidationError."""
    if verdict == RowReviewDecision.modify and modified_score is None:
        raise ReviewValidationError("modify requires modified_score")
    entry = _build_cache_entry(
        project=project, stage_id=stage_id,
        stage_fingerprint=stage_fingerprint, input_fingerprint=input_fingerprint,
        frozen_row=frozen_row, verdict=verdict, modified_score=modified_score,
        reviewer=reviewer, reviewed_at=reviewed_at,
    )
    StageCacheEntry.for_mode(RunMode.PRODUCTION).put(entry)


def _build_cache_entry(
    *, project: str, stage_id: str,
    stage_fingerprint: str, input_fingerprint: str,
    frozen_row: Mapping[str, object],
    verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> StageCacheEntry:
    """A `StageCacheEntry` for this reviewer decision, generalized to the cache's
    payload: fingerprints are the ones the caller resolved (never recomputed
    here), `frozen_input` the reviewed row reduced to JSON-native types, and
    `output_row` the review stage's output for it (None as a tombstone for a
    `reject`). The cache stores no review vocabulary — the verdict survives only
    as columns on the output row."""
    frozen_safe = to_json_safe_row({str(k): v for k, v in frozen_row.items()})
    output_row = _build_output_row(frozen_safe, verdict, modified_score, reviewer, reviewed_at)
    return StageCacheEntry(
        id=build_cache_id(project, stage_id, stage_fingerprint, input_fingerprint),
        project=project,
        stage_id=stage_id,
        stage_fingerprint=stage_fingerprint,
        input_fingerprint=input_fingerprint,
        frozen_input=frozen_safe,
        output_row=output_row,
    )


def _build_output_row(
    frozen_safe: JsonDict, verdict: RowReviewDecision, modified_score: float | None,
    reviewer: str, reviewed_at: str,
) -> JsonDict | None:
    """The review stage's output row for one reviewed input: the frozen input
    plus the score columns the verdict produces. A `reject` drops the row, so
    its output is None (a tombstone); `approve` keeps the AI score as final,
    `modify` substitutes the human-entered score."""
    if verdict == RowReviewDecision.reject:
        return None
    ai = frozen_safe.get("score")
    human = modified_score if verdict == RowReviewDecision.modify else ai
    return {
        **frozen_safe,
        "ai_score": ai, "human_score": human, "final_score": human,
        "review_notes": f"decision={verdict.value}",
        "reviewer_id": reviewer, "reviewed_at": reviewed_at,
        "decision": verdict.value,
    }
