"""The claim tool bodies; app.tools.shared sits at its import ceiling."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.json_types import JsonDict
from app.models.records.claim_review import Challenge
from app.models.records.claims import Claim
from app.services import claim_review as claim_review_service
from app.services import claim_review_run
from app.services import claims as claims_service
from app.tools.shared import validate_project_exists


class ClaimReviewRead(BaseModel):
    """What a review came to, or why there is nothing to read yet."""

    claim_id: str
    claim_text: str
    status: str
    review: str
    error: str | None = None
    challenges: list[Challenge] = []


def submit_claim(
    project_id: str, run_id: str, slug: str, text: str, context: JsonDict | None = None
) -> Claim:
    validate_project_exists(project_id)
    claim = claims_service.submit_claim(project_id, run_id, slug, context or {}, text)
    claim_review_run.start_claim_review(project_id, claim.id, model="sonnet")
    return claim


def read_claim_review(project_id: str, claim_id: str) -> ClaimReviewRead:
    validate_project_exists(project_id)
    claim = claims_service.load_claim(project_id, claim_id)
    review = claim_review_service.load_claim_review(claim_id)
    state = claim_review_run.read_review_state(claim, review)
    return ClaimReviewRead(
        claim_id=claim.id, claim_text=claim.text, status=claim.status,
        review=state.review, error=state.error,
        challenges=list(review.challenges) if review is not None else [],
    )


def cancel_claim_submission(project_id: str, claim_id: str) -> Claim:
    validate_project_exists(project_id)
    return claims_service.decline_claim(project_id, claim_id)
