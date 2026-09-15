"""A review is written once. A re-review is a new claim and a new review."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.errors import ClaimReviewIsImmutable
from app.models.records.claim_review import ClaimPart, ClaimReview

_FIELDS = dict(
    claim_id="c1", claim_parts=[ClaimPart(phrase="Grants")], challenges=[],
    summary="Nothing in the run backs it.", session_ids=["session-parts", "session-data"],
)


def _a_review() -> ClaimReview:
    return ClaimReview.model_validate(_FIELDS)


def test_a_review_is_stored_and_read_back_by_claim():
    review = _a_review()
    review.save()

    [held] = ClaimReview.find(claim_id="c1")
    assert held.summary == review.summary and held.claim_parts == review.claim_parts
    assert held.session_ids == ["session-parts", "session-data"]


def test_a_review_names_no_project_and_no_run_of_its_own():
    assert not {"project_id", "run_id", "grounding", "proposed_rewrites"} & set(
        ClaimReview.model_fields)


def test_the_fields_below_are_every_field_a_review_declares():
    assert set(_FIELDS) == set(ClaimReview.model_fields) - {"id", "created_at", "updated_at"}
    assert "session_ids" in _FIELDS


@pytest.mark.parametrize("left_out", [name for name in _FIELDS])
def test_every_field_is_required(left_out):
    with pytest.raises(ValidationError):
        ClaimReview.model_validate({k: v for k, v in _FIELDS.items() if k != left_out})


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
