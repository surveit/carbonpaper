"""Dropping the repeats after the five reviewers answer: a post-processing step, not a turn
that judges. It may remove a challenge and do nothing else."""
from __future__ import annotations

import json

from app.core.agent.agent import Agent
from app.core.errors import ClaimReviewFailed
from app.models.claim_review import DedupeAnswer, EvidenceBundle
from app.models.records.claim_review import DraftChallenge
from app.reviewer.dedupe_prompt import DEDUPE_SYSTEM_PROMPT
from app.reviewer.evidence import render_evidence_bundle

DEDUPE_REQUEST = "drop the challenges that repeat another"

_CHALLENGES_HEADING = "----- CHALLENGES RAISED -----"


def build_deduper(
    bundle: EvidenceBundle, raised: list[DraftChallenge], *, model: str = "sonnet"
) -> Agent[DedupeAnswer]:
    return Agent(system_prompt=DEDUPE_SYSTEM_PROMPT, target_schema=DedupeAnswer,
                 task=render_dedupe_task(bundle, raised), model=model)


def render_dedupe_task(bundle: EvidenceBundle, raised: list[DraftChallenge]) -> str:
    return "\n\n".join([
        f"{DEDUPE_REQUEST} — the sentence is quoted below with what the run holds, then "
        "every challenge raised against it, numbered from 0.",
        render_evidence_bundle(bundle),
        _render_challenges(raised),
    ])


def keep_what_is_not_a_repeat(
    raised: list[DraftChallenge], answer: DedupeAnswer
) -> list[DraftChallenge]:
    """Only removal: a challenge kept is the one its reviewer wrote, untouched."""
    dropped = {one.index for one in answer.drop}
    _refuse_a_drop_that_names_nothing(raised, answer, dropped)
    return [one for index, one in enumerate(raised) if index not in dropped]


def _refuse_a_drop_that_names_nothing(
    raised: list[DraftChallenge], answer: DedupeAnswer, dropped: set[int]
) -> None:
    for one in answer.drop:
        if not 0 <= one.index < len(raised):
            raise ClaimReviewFailed(
                f"the deduper dropped challenge {one.index}, which was never raised")
        if not 0 <= one.duplicate_of < len(raised):
            raise ClaimReviewFailed(
                f"challenge {one.index} repeats {one.duplicate_of}, which was never raised")
        if one.duplicate_of in dropped:
            raise ClaimReviewFailed(
                f"challenge {one.index} repeats {one.duplicate_of}, which is also dropped")
        if one.duplicate_of == one.index:
            raise ClaimReviewFailed(f"challenge {one.index} was called a repeat of itself")


def _render_challenges(raised: list[DraftChallenge]) -> str:
    numbered = [{"index": index, **one.model_dump(mode="json")}
                for index, one in enumerate(raised)]
    return f"{_CHALLENGES_HEADING}\n{json.dumps(numbered, indent=2)}"
