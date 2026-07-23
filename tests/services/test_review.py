"""Tests for app/services/review.py: the decision layer behind the reviewer
web route — verdict validation (parse_verdict), cache writes (record_decision),
and prior-decision loading (load_prior_decisions), all through the production
stage-result cache accessor."""
from __future__ import annotations

import pytest

from app.core.errors import ReviewValidationError
from app.models import RowReviewDecision
from app.services import review
from app.services.review import PriorDecision


# ── parse_verdict ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("verdict", ["approve", "reject"])
def test_parse_verdict_non_modify_returns_verdict_and_no_score(verdict):
    parsed, score = review.parse_verdict(verdict, None)
    assert parsed == RowReviewDecision(verdict)
    assert score is None


def test_parse_verdict_modify_parses_the_numeric_score():
    parsed, score = review.parse_verdict("modify", "99")
    assert parsed == RowReviewDecision.modify
    assert score == 99.0


def test_parse_verdict_rejects_an_unknown_verdict():
    with pytest.raises(ReviewValidationError):
        review.parse_verdict("shrug", None)


@pytest.mark.parametrize("bad_score", [None, "", "not-a-number"])
def test_parse_verdict_rejects_modify_without_a_numeric_score(bad_score):
    with pytest.raises(ReviewValidationError):
        review.parse_verdict("modify", bad_score)


# ── record_decision → load_prior_decisions ───────────────────────────────────

def test_record_decision_then_load_prior_decisions_roundtrips():
    review.record_decision(
        project="proj", stage_id="review", run_id="run1",
        stage_fingerprint="sf1", input_fingerprint="if1",
        frozen_row={"id": "a", "score": 1},
        verdict=RowReviewDecision.modify, modified_score=42.0,
        reviewer="local", reviewed_at="2026-07-22T10:00:00",
    )

    prior = review.load_prior_decisions("proj", "review", "sf1")
    assert set(prior) == {"if1"}
    decision = prior["if1"]
    assert isinstance(decision, PriorDecision)
    assert decision.decision == "modify"
    assert decision.modified_score == 42.0
    assert decision.reviewer == "local"
    assert decision.reviewed_at == "2026-07-22T10:00:00"

    # A different stage_fingerprint scopes out this decision entirely.
    assert review.load_prior_decisions("proj", "review", "sf-other") == {}
