from __future__ import annotations

import pytest

from app.core.errors import ReviewValidationError
from app.models import ReviewVerdict
from app.services import review
from app.core.stage_cache import StageCacheEntry


def _load_entry(input_fingerprint: str):
    return StageCacheEntry.read_only().get(
        "proj", "review", "sf1", input_fingerprint
    )


def test_record_decision_writes_the_output_row_the_modify_verdict_produces():
    review.record_decision(
        project="proj", stage_id="review",
        stage_fingerprint="sf1", input_fingerprint="if1",
        frozen_row={"id": "a", "score": 1},
        verdict=ReviewVerdict.modify, modified_score=42.0,
        reviewer="local", reviewed_at="2026-07-22T10:00:00",
    )

    entry = _load_entry("if1")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["decision"] == "modify"
    assert entry.output_row["final_score"] == 42.0
    assert entry.output_row["reviewer_id"] == "local"
    assert entry.output_row["reviewed_at"] == "2026-07-22T10:00:00"


def test_record_decision_writes_the_output_row_the_approve_verdict_produces():
    """An `approve` keeps the AI score as the score everyone stands behind, and
    carries the frozen input columns through unchanged."""
    review.record_decision(
        project="proj", stage_id="review",
        stage_fingerprint="sf1", input_fingerprint="if2",
        frozen_row={"id": "b", "score": 1},
        verdict=ReviewVerdict.approve, modified_score=None,
        reviewer="local", reviewed_at="2026-07-22T10:00:00",
    )

    entry = _load_entry("if2")
    assert entry is not None
    assert entry.output_row is not None
    assert entry.output_row["decision"] == "approve"
    assert entry.output_row["id"] == "b"
    assert entry.output_row["ai_score"] == 1
    assert entry.output_row["human_score"] == 1
    assert entry.output_row["final_score"] == 1
    assert entry.output_row["reviewer_id"] == "local"
    assert entry.output_row["reviewed_at"] == "2026-07-22T10:00:00"


def test_record_decision_rejects_modify_without_a_score():
    with pytest.raises(ReviewValidationError):
        review.record_decision(
            project="proj", stage_id="review",
            stage_fingerprint="sf1", input_fingerprint="if3",
            frozen_row={"id": "c", "score": 1},
            verdict=ReviewVerdict.modify, modified_score=None,
            reviewer="local", reviewed_at="2026-07-22T10:00:00",
        )
    # Nothing was written for the rejected input.
    assert _load_entry("if3") is None
