"""Starting a claim's review, and storing what the reviewers answered."""
from __future__ import annotations

from app.core.agent.store import AgentSession
from app.core.ids import ID
from app.models.claim_review import ClaimReviewResult
from app.models.claims import ClaimStatus
from app.reviewer.run import PARENT_ROLE, start_claim_review_agents
from app.services import claims as claims_service
from app.services.claim_review import (
    build_evidence_bundle,
    load_claim_review,
    store_claim_review,
)
from app.services.errors import ClaimReviewRefused


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
