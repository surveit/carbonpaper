"""A review is written once. A re-attack is a new claim and a new review."""
from __future__ import annotations

import pytest

from app.core.errors import ClaimReviewIsImmutable
from app.models.claim_review import Grounding
from app.models.records.claim_review import ClaimReview


def _a_review() -> ClaimReview:
    return ClaimReview(project_id="p", claim_id="c1", run_id="r1",
                       grounding=[Grounding(start=0, end=5, evidence=None, how="nothing")],
                       challenges=[], summary="Nothing in the run backs it.")


def test_a_review_is_stored_and_read_back_by_claim():
    review = _a_review()
    review.save()
    assert ClaimReview.find(claim_id="c1")[0].summary == review.summary


def test_saving_a_review_again_is_refused():
    review = _a_review()
    review.save()
    with pytest.raises(ClaimReviewIsImmutable):
        ClaimReview.load(review.id).save()


def test_deleting_one_is_allowed_and_a_new_one_may_follow():
    review = _a_review()
    review.save()
    ClaimReview.delete(review.id)
    _a_review().save()
    assert len(ClaimReview.find(claim_id="c1")) == 1
