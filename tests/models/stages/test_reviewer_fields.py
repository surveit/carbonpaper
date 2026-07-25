"""Tests for `find_reviewer_fields` (app/models/stages/human_review_queue.py):
what a human_review_queue stage asks its reviewer for is DERIVED from its own
output_schema — the columns it declares beyond the row the reviewer was
handed, minus the ones the review service fills itself. No score vocabulary is
hardcoded anywhere in the derivation; a stage that declares `verdict_text` and
`confidence` asks for exactly those."""
from __future__ import annotations

from app.models import Stage
from app.models.stages import SERVICE_FILLED_COLUMNS, find_reviewer_fields


def _stage(output_columns=None):
    spec = {
        "id": "review", "name": "Review", "type": "human_review_queue",
        "inputs": [{"id": "scored"}], "queue": {},
    }
    if output_columns is not None:
        spec["output_schema"] = {"columns": output_columns}
    return Stage.model_validate(spec)


def test_reviewer_fields_are_the_declared_columns_the_input_does_not_carry():
    stage = _stage([
        {"name": "id", "type": "str"},
        {"name": "score", "type": "int"},
        {"name": "final_score", "type": "int"},
        {"name": "review_notes", "type": "str"},
    ])

    fields = find_reviewer_fields(stage, ["id", "score"])

    assert [c.name for c in fields] == ["final_score", "review_notes"]


def test_reviewer_fields_carry_no_score_vocabulary_of_their_own():
    """The derivation knows no column names: a stage reviewing prose asks for
    the prose columns it declared, not for a score."""
    stage = _stage([
        {"name": "claim_id", "type": "str"},
        {"name": "verdict_text", "type": "str"},
        {"name": "confidence", "type": "float"},
    ])

    fields = find_reviewer_fields(stage, ["claim_id", "quote"])

    assert [c.name for c in fields] == ["verdict_text", "confidence"]
    assert [c.type for c in fields] == ["str", "float"]


def test_service_filled_columns_are_never_asked_of_the_reviewer():
    stage = _stage([{"name": name, "type": "str"} for name in sorted(SERVICE_FILLED_COLUMNS)]
                   + [{"name": "final_score", "type": "int"}])

    fields = find_reviewer_fields(stage, ["id"])

    assert [c.name for c in fields] == ["final_score"]


def test_a_stage_with_no_output_schema_asks_for_nothing():
    assert find_reviewer_fields(_stage(), ["id", "score"]) == []


def test_declaration_order_is_preserved():
    stage = _stage([
        {"name": "b_field", "type": "str"},
        {"name": "a_field", "type": "str"},
        {"name": "id", "type": "str"},
    ])

    assert [c.name for c in find_reviewer_fields(stage, ["id"])] == ["b_field", "a_field"]
