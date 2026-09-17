"""Building one of the five reviewers over a claim's bundle, and the task it reads."""
from __future__ import annotations

from app.core.agent.agent import Agent
from app.core.errors import ClaimReviewFailed
from app.models.claim_review import ChallengesAnswer, EvidenceBundle, Reviewer
from app.reviewer.evidence import render_evidence_bundle
from app.reviewer.reviewers_prompt import (
    CHOICES_SYSTEM_PROMPT,
    COVERAGE_SYSTEM_PROMPT,
    DATA_DEFECTS_SYSTEM_PROMPT,
    MEANING_SYSTEM_PROMPT,
    OMISSIONS_SYSTEM_PROMPT,
)

# What the click asks for; the claim and what the run holds follow it.
REVIEW_REQUEST = "review this claim"

_SYSTEM_PROMPTS: dict[Reviewer, str] = {
    Reviewer.data_defects: DATA_DEFECTS_SYSTEM_PROMPT,
    Reviewer.choices: CHOICES_SYSTEM_PROMPT,
    Reviewer.omissions: OMISSIONS_SYSTEM_PROMPT,
    Reviewer.coverage: COVERAGE_SYSTEM_PROMPT,
    Reviewer.meaning: MEANING_SYSTEM_PROMPT,
}

REVIEWERS: tuple[Reviewer, ...] = tuple(_SYSTEM_PROMPTS)


def build_reviewer(
    reviewer: Reviewer, bundle: EvidenceBundle, *, model: str = "sonnet"
) -> Agent[ChallengesAnswer]:
    return Agent(system_prompt=_read_system_prompt(reviewer), target_schema=ChallengesAnswer,
                 task=render_review_task(bundle), model=model)


def render_review_task(bundle: EvidenceBundle) -> str:
    """Every reviewer is handed the same pool; what it reads that pool for is its prompt."""
    return "\n\n".join([
        f"{REVIEW_REQUEST} — the sentence is quoted below with what the run holds. "
        "Return your answer with submit_answer.",
        render_evidence_bundle(bundle),
    ])


def _read_system_prompt(reviewer: Reviewer) -> str:
    if reviewer not in _SYSTEM_PROMPTS:
        raise ClaimReviewFailed(f"`{reviewer.value}` is not one of the five reviewers")
    return _SYSTEM_PROMPTS[reviewer]
