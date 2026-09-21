"""Starting a claim's review, and storing what the reviewers answered."""
from __future__ import annotations

from pydantic import BaseModel

from app.core.agent.store import AgentSession, SessionStore
from app.core.ids import ID
from app.models.claim_review import ClaimReviewResult
from app.models.citations import StageOutputCellCitation
from app.models.claims import ClaimStatus
from app.models.records.claim_review import ClaimReview
from app.models.records.claims import Claim
from app.reviewer.run import PARENT_ROLE, start_claim_review_agents
from app.services import claims as claims_service
from app.services.claim_review import (
    build_evidence_bundle,
    load_claim_review,
    store_claim_review,
)
from app.services.errors import ClaimReviewRefused
from app.services.generation import GENERATION_FAILURE_PREFIX


REVIEW_NONE = "none"
REVIEW_RUNNING = "running"
REVIEW_FAILED = "failed"
REVIEW_DONE = "done"
REVIEW_REFUSED = "refused"


class ReviewState(BaseModel):
    """Where a claim's review stands, for whoever asks: a page, or an agent."""

    review: str
    error: str | None = None
    session_id: ID | None = None


def read_review_state(claim: Claim, review: ClaimReview | None) -> ReviewState:
    if review is not None:
        return ReviewState(review=REVIEW_DONE, session_id=review.session_id)
    if not isinstance(claim.citation, StageOutputCellCitation):
        return ReviewState(review=REVIEW_REFUSED)
    reviewed = find_review_sessions(claim.id)
    if not reviewed:
        return ReviewState(review=REVIEW_NONE)
    return _read_session_state(reviewed[0])


def _read_session_state(session: AgentSession) -> ReviewState:
    if session.active_turn is not None:
        return ReviewState(review=REVIEW_RUNNING, session_id=session.id)
    failure = _find_generation_failure(session.id)
    if failure is None:
        return ReviewState(review=REVIEW_NONE, session_id=session.id)
    return ReviewState(review=REVIEW_FAILED, error=failure, session_id=session.id)


def _find_generation_failure(session_id: ID) -> str | None:
    return next(
        (text for text in SessionStore().read_last_reply_texts(session_id)
         if text.startswith(GENERATION_FAILURE_PREFIX)),
        None,
    )


def start_claim_review(project_id: ID, claim_id: ID, *, model: str) -> str:
    """Must be called from the server event loop — the turns run as a task there."""
    claim = claims_service.load_claim(project_id, claim_id)
    _refuse_a_claim_already_reviewed(claim_id)
    _refuse_a_claim_already_under_review(claim_id)
    if claim.status != ClaimStatus.submitted:
        raise ClaimReviewRefused(
            [f"claim {claim_id} is {claim.status}; only a submitted claim is reviewed"])
    return start_claim_review_agents(
        project_id=project_id, bundle=build_evidence_bundle(project_id, claim_id), model=model,
        on_answer=lambda result: _finish_claim_review(project_id, claim_id, result),
    )


def _finish_claim_review(project_id: ID, claim_id: ID, result: ClaimReviewResult) -> None:
    store_claim_review(project_id, claim_id, challenges=result.challenges,
                       session_id=result.session_id)


def _refuse_a_claim_already_reviewed(claim_id: ID) -> None:
    if load_claim_review(claim_id) is not None:
        raise ClaimReviewRefused(
            [f"claim {claim_id} already has a review; a re-review is a new claim"])


def _refuse_a_claim_already_under_review(claim_id: ID) -> None:
    """Two runs would write two reviews of one claim, and the second is refused storage."""
    if _find_running_reviews(claim_id):
        raise ClaimReviewRefused(
            [f"claim {claim_id} is already being reviewed; it is reviewed once at a time"])


def find_review_sessions(claim_id: ID) -> list[AgentSession]:
    """Every review of this claim, newest first; a running one still holds an active turn."""
    held = [session for session in AgentSession.list()
            if session.context.get("role") == PARENT_ROLE
            and session.context.get("claim_id") == claim_id]
    return sorted(held, key=lambda session: (session.created_at, session.id), reverse=True)


def _find_running_reviews(claim_id: ID) -> list[AgentSession]:
    return [session for session in AgentSession.list()
            if session.context.get("role") == PARENT_ROLE
            and session.context.get("claim_id") == claim_id
            and session.active_turn is not None]
