"""Tests for app/services/review.py: recording a reviewer verdict as a
stage-result cache entry (record_decision), through the production
stage-result cache accessor.

The output row is built from the STAGE'S OWN output_schema — the columns it
declares beyond the queued row are the fields the reviewer supplies — so these
tests declare different schemas and assert the recorded row follows the
declaration, not a fixed ai_score/human_score/final_score shape. Coercion and
the schema's nullability/enum/range rules live here too: the web boundary
knows only that a refused submission is a 400."""
from __future__ import annotations

import pytest

from app.core.errors import ReviewValidationError
from app.core.stage_cache import StageCacheEntry
from app.models import RowReviewDecision, Stage
from app.services import review


def _stage(output_columns=None):
    spec = {
        "id": "review", "name": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}], "queue": {},
    }
    if output_columns is not None:
        spec["output_schema"] = {"columns": output_columns}
    return Stage.model_validate(spec)


def _record(stage, input_fingerprint, verdict, fields, frozen_row=None):
    review.record_decision(
        project="proj", stage=stage,
        stage_fingerprint="sf1", input_fingerprint=input_fingerprint,
        frozen_row=frozen_row if frozen_row is not None else {"id": "a", "score": 1},
        verdict=verdict, submitted_fields=fields,
        reviewer="local", reviewed_at="2026-07-22T10:00:00",
    )


def _load_entry(input_fingerprint):
    return StageCacheEntry.read_only().get("proj", "review", "sf1", input_fingerprint)


# ── The declared fields are what gets recorded ───────────────────────────────


def test_output_row_is_the_frozen_input_plus_the_declared_fields():
    stage = _stage([
        {"name": "id", "type": "str"}, {"name": "score", "type": "int"},
        {"name": "final_score", "type": "int"}, {"name": "review_notes", "type": "str"},
        {"name": "decision", "type": "str"},
    ])

    _record(stage, "if1", RowReviewDecision.modify,
            {"final_score": "42", "review_notes": "downgraded, weak sourcing"})

    entry = _load_entry("if1")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row == {
        "id": "a", "score": 1,                       # the frozen input, carried through
        "final_score": 42,                           # coerced to its declared int
        "review_notes": "downgraded, weak sourcing",
        "decision": "modify", "reviewer_id": "local",
        "reviewed_at": "2026-07-22T10:00:00",
    }


def test_a_stage_declaring_prose_fields_records_prose_not_scores():
    """Nothing in the service knows the word "score": a stage whose reviewer
    supplies a verdict sentence and a confidence gets exactly those columns."""
    stage = _stage([
        {"name": "claim_id", "type": "str"},
        {"name": "verdict_text", "type": "str"},
        {"name": "confidence", "type": "float"},
    ])

    _record(stage, "if2", RowReviewDecision.modify,
            {"verdict_text": "unsupported", "confidence": "0.25"},
            frozen_row={"claim_id": "c1"})

    entry = _load_entry("if2")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["verdict_text"] == "unsupported"
    assert entry.output_row["confidence"] == 0.25
    assert "final_score" not in entry.output_row


def test_declared_fields_left_blank_are_recorded_as_null():
    stage = _stage([{"name": "id", "type": "str"}, {"name": "final_score", "type": "int"}])

    _record(stage, "if3", RowReviewDecision.approve, {})

    entry = _load_entry("if3")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["final_score"] is None      # nobody supplied one
    assert entry.output_row["decision"] == "approve"


def test_a_stage_declaring_no_output_schema_records_input_plus_audit_only():
    _record(_stage(), "if4", RowReviewDecision.approve, {})

    entry = _load_entry("if4")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row == {
        "id": "a", "score": 1,
        "decision": "approve", "reviewer_id": "local",
        "reviewed_at": "2026-07-22T10:00:00",
    }


def test_reject_writes_a_tombstone_whatever_the_schema_declares():
    stage = _stage([{"name": "id", "type": "str"},
                    {"name": "final_score", "type": "int", "nullable": False}])

    _record(stage, "if5", RowReviewDecision.reject, {})

    entry = _load_entry("if5")
    assert entry is not None
    assert entry.output_row is None  # a reject drops the row — nothing is asked of it


# ── Coercion to the declared type ────────────────────────────────────────────


def test_values_are_coerced_to_their_declared_types():
    stage = _stage([
        {"name": "id", "type": "str"},
        {"name": "n", "type": "int"}, {"name": "x", "type": "float"},
        {"name": "flag", "type": "bool"}, {"name": "note", "type": "str"},
        {"name": "extra", "type": "json", "value_type": "str"},
    ])

    _record(stage, "if6", RowReviewDecision.modify, {
        "n": "7", "x": "1.5", "flag": "no", "note": " kept ", "extra": '{"k": "v"}',
    })

    entry = _load_entry("if6")
    assert entry is not None and entry.output_row is not None
    assert entry.output_row["n"] == 7
    assert entry.output_row["x"] == 1.5
    assert entry.output_row["flag"] is False
    assert entry.output_row["note"] == "kept"
    assert entry.output_row["extra"] == {"k": "v"}


def test_a_value_that_will_not_coerce_is_refused_and_writes_nothing():
    stage = _stage([{"name": "id", "type": "str"}, {"name": "final_score", "type": "int"}])

    with pytest.raises(ReviewValidationError, match="not a valid int"):
        _record(stage, "if7", RowReviewDecision.modify, {"final_score": "not-a-number"})
    assert _load_entry("if7") is None


# ── The schema's own rules are the validation rules ──────────────────────────


def test_a_required_field_left_blank_is_refused():
    stage = _stage([{"name": "id", "type": "str"},
                    {"name": "final_score", "type": "int", "nullable": False}])

    with pytest.raises(ReviewValidationError, match="required"):
        _record(stage, "if8", RowReviewDecision.approve, {})
    assert _load_entry("if8") is None


def test_a_value_outside_the_declared_enum_is_refused():
    stage = _stage([{"name": "id", "type": "str"},
                    {"name": "verdict", "type": "str", "enum": ["sound", "unsound"]}])

    with pytest.raises(ReviewValidationError, match="not one of the declared values"):
        _record(stage, "if9", RowReviewDecision.modify, {"verdict": "maybe"})
    assert _load_entry("if9") is None


def test_a_value_outside_the_declared_range_is_refused():
    stage = _stage([{"name": "id", "type": "str"},
                    {"name": "final_score", "type": "int", "range": [-2, 2]}])

    with pytest.raises(ReviewValidationError, match="above the declared range"):
        _record(stage, "if10", RowReviewDecision.modify, {"final_score": "9"})
    assert _load_entry("if10") is None


def test_a_field_the_schema_does_not_declare_is_refused_not_dropped():
    stage = _stage([{"name": "id", "type": "str"}, {"name": "final_score", "type": "int"}])

    with pytest.raises(ReviewValidationError, match="not declared by its output_schema"):
        _record(stage, "if11", RowReviewDecision.modify,
                {"final_score": "3", "invented_column": "x"})
    assert _load_entry("if11") is None


def test_modify_must_change_at_least_one_declared_field():
    """The general form of the old "modify requires modified_score": whatever
    the stage declares, a modify supplying none of it is refused."""
    stage = _stage([{"name": "id", "type": "str"}, {"name": "final_score", "type": "int"}])

    with pytest.raises(ReviewValidationError, match="at least one"):
        _record(stage, "if12", RowReviewDecision.modify, {})
    assert _load_entry("if12") is None
