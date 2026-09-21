"""The eval seam: one row names a claim, and the challenges come back as row data."""
from __future__ import annotations

import json
from typing import Any

import pytest

import app.reviewer.run as reviewer_run
from app.core.agent.store import SessionStore
from app.models.citations import StageOutputColumnCitation
from app.models.claim_review import ChallengesAnswer
from app.models.records.claim_review import (
    ChallengeKind,
    ClaimPart,
    DraftChallenge,
    Severity,
)
from app.reviewer.dedupe import DedupeAnswer
from app.reviewer.reviewers import REVIEWERS
from app.services.claim_review_eval import review_the_claim_a_row_names
from app.services.errors import ClaimRefused
from claim_review_fixture import PROJECT, claim_the_total, run_the_fixture


class _FakeAgent:
    def __init__(self, answer: ChallengesAnswer | DedupeAnswer) -> None:
        self.answer = answer
        self.last_usage = None

    async def run(self, emit: Any = None) -> ChallengesAnswer | DedupeAnswer:
        return self.answer


def _one_challenge() -> ChallengesAnswer:
    return ChallengesAnswer(challenges=[DraftChallenge(
        kind=ChallengeKind.data,
        claim_part=ClaimPart(phrase="Grants"),
        text="The figure counts rows, not grants.",
        justification="the amount column is blank",
        citations=[StageOutputColumnCitation(
            run_id="r", stage_id="grant_totals", column="grants")],
        severity=Severity.high)])


@pytest.fixture
def claim_id(projects_root, monkeypatch) -> str:
    monkeypatch.setattr(
        reviewer_run, "build_reviewer",
        lambda reviewer, bundle, *, model="sonnet": _FakeAgent(_one_challenge()))
    monkeypatch.setattr(
        reviewer_run, "build_deduper",
        lambda bundle, raised, *, model="sonnet": _FakeAgent(DedupeAnswer(drop=[])))
    return claim_the_total(run_the_fixture(projects_root)).id


def _review(claim_id: str, **named: str) -> dict:
    return review_the_claim_a_row_names(
        {"project_id": PROJECT, "claim_id": claim_id, "model": "sonnet", **named})


def test_a_reviewed_claim_comes_back_as_one_row_of_challenges(claim_id) -> None:
    row = _review(claim_id)

    assert row["challenge_count"] == len(REVIEWERS)
    challenges = json.loads(row["challenges_json"])
    assert len(challenges) == len(REVIEWERS)
    assert challenges[0] == {
        "kind": "data",
        "claim_part": "Grants",
        "claim_part_occurrence": 1,
        "text": "The figure counts rows, not grants.",
        "justification": "the amount column is blank",
        "severity": "high",
    }


def test_a_claim_the_project_does_not_hold_raises(claim_id) -> None:
    with pytest.raises(ClaimRefused, match="no claim"):
        _review("no-such-claim")


def test_a_row_naming_no_claim_raises(claim_id) -> None:
    with pytest.raises(ValueError, match="carries no 'claim_id'"):
        review_the_claim_a_row_names({"project_id": PROJECT})


def test_the_session_it_opens_carries_none_of_the_parent_markers(claim_id) -> None:
    row = _review(claim_id)

    session = SessionStore().load(row["review_session_id"])
    assert session["context"].get("role") is None
    assert session["context"].get("claim_id") is None
    assert session["pending_user"] is None
    assert session["active_turn"] is None
