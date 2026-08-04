from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.models.stages.human_review_queue import find_queue_column_issues

_INPUT_COLUMNS = [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "assertion_text", "type": "str", "nullable": False},
    {"name": "score", "type": "int", "nullable": True},
    {"name": "confidence", "type": "int", "nullable": True},
]
_OUTPUT_COLUMNS = [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "assertion_text", "type": "str", "nullable": False},
    {"name": "score", "type": "int", "nullable": True},
    {"name": "human_score", "type": "int", "nullable": True},
    {"name": "decision", "type": "str", "nullable": True},
    {"name": "reviewer_id", "type": "str", "nullable": True},
    {"name": "reviewed_at", "type": "str", "nullable": True},
]
_QUEUE = {
    "reviewed_columns": {"score": "human_score"},
    "verdict_column": "decision",
    "reviewer_column": "reviewer_id",
    "reviewed_at_column": "reviewed_at",
}


def _stage_spec(*, queue=None, input_columns=None, output_columns=None):
    return {
        "id": "wc", "type": "human_review_queue", "name": "wc",
        "inputs": [{"id": "src", "schema": {"columns": input_columns or _INPUT_COLUMNS}}],
        "output_schema": {"columns": output_columns or _OUTPUT_COLUMNS},
        "queue": {**_QUEUE, **(queue or {})},
    }


# ── the valid config ────────────────────────────────────────────────────────


def test_a_fully_valid_config_reports_nothing():
    stage = parse_stage(_stage_spec())
    assert find_queue_column_issues(stage) == []


# ── 1. the filter predicate ──────────────────────────────────────────────────


def test_filter_naming_a_column_the_input_lacks_is_rejected():
    with pytest.raises(ValidationError, match="writer_confirmed"):
        parse_stage(_stage_spec(queue={"filter": "writer_confirmed == True"}))


def test_a_filter_over_input_columns_is_clean():
    stage = parse_stage(_stage_spec(queue={"filter": "assertion_text IS NOT NULL"}))
    assert find_queue_column_issues(stage) == []


# ── 2. reviewed source columns ───────────────────────────────────────────────


def test_a_reviewed_source_absent_from_the_input_is_rejected():
    with pytest.raises(ValidationError, match="ghost"):
        parse_stage(_stage_spec(queue={"reviewed_columns": {"ghost": "human_ghost"}}))


def test_a_non_scalar_reviewed_source_is_rejected():
    """A `json` column cannot be answered through a form field, so it cannot be
    a reviewed source."""
    input_columns = _INPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str", "nullable": True},
    ]
    output_columns = _OUTPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str", "nullable": True},
        {"name": "human_evidence", "type": "json", "value_type": "str", "nullable": True},
    ]
    with pytest.raises(ValidationError, match="evidence"):
        parse_stage(_stage_spec(
            queue={"reviewed_columns": {"score": "human_score", "evidence": "human_evidence"}},
            input_columns=input_columns, output_columns=output_columns,
        ))


# ── 3. reviewed target columns on output_schema ──────────────────────────────


def test_a_reviewed_target_missing_from_output_schema_is_rejected():
    with pytest.raises(ValidationError, match="human_verdict_score"):
        parse_stage(_stage_spec(
            queue={"reviewed_columns": {"score": "human_verdict_score"}}))


def test_a_reviewed_target_of_the_wrong_type_is_rejected():
    output_columns = [
        c if c["name"] != "human_score" else {"name": "human_score", "type": "str", "nullable": True}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="human_score"):
        parse_stage(_stage_spec(output_columns=output_columns))


def test_a_reviewed_target_less_permissive_than_its_source_is_rejected():
    """The source may be null, so a non-nullable target could not hold it."""
    output_columns = [
        c if c["name"] != "human_score"
        else {"name": "human_score", "type": "int", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="human_score"):
        parse_stage(_stage_spec(output_columns=output_columns))


def test_a_reviewed_target_more_permissive_than_its_source_is_clean():
    input_columns = [
        c if c["name"] != "score" else {"name": "score", "type": "int", "nullable": False}
        for c in _INPUT_COLUMNS
    ]
    stage = parse_stage(_stage_spec(input_columns=input_columns))
    assert find_queue_column_issues(stage) == []


# ── 4. the review-record columns on output_schema ────────────────────────────


def test_a_review_record_column_missing_from_output_schema_is_rejected():
    with pytest.raises(ValidationError, match="who_reviewed"):
        parse_stage(_stage_spec(queue={"reviewer_column": "who_reviewed"}))


def test_a_review_record_column_declared_non_str_is_rejected():
    output_columns = [
        c if c["name"] != "decision" else {"name": "decision", "type": "int", "nullable": True}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="decision"):
        parse_stage(_stage_spec(output_columns=output_columns))


def test_a_declared_notes_column_must_be_declared_on_output_schema():
    with pytest.raises(ValidationError, match="review_notes"):
        parse_stage(_stage_spec(queue={"review_notes_column": "review_notes"}))


def test_a_declared_notes_column_present_on_output_schema_is_clean():
    stage = parse_stage(_stage_spec(
        queue={"review_notes_column": "review_notes"},
        output_columns=_OUTPUT_COLUMNS + [{"name": "review_notes", "type": "str", "nullable": True}],
    ))
    assert find_queue_column_issues(stage) == []


def test_a_non_nullable_review_record_column_is_rejected():
    # The runtime writes no reviewer into a filter-skipped or auto-approved row, so a non-
    # nullable declaration would fail at the END of a run — after the human had done all
    # the reviewing.
    output_columns = [
        c if c["name"] != "reviewer_id"
        else {"name": "reviewer_id", "type": "str", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="non-nullable"):
        parse_stage(_stage_spec(output_columns=output_columns))


def test_a_non_nullable_verdict_column_is_clean():
    """The one review-record column the runtime writes on every row."""
    output_columns = [
        c if c["name"] != "decision"
        else {"name": "decision", "type": "str", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    stage = parse_stage(_stage_spec(output_columns=output_columns))
    assert find_queue_column_issues(stage) == []


# ── 5. no added column may collide with an input column ──────────────────────


def test_an_added_column_that_the_input_already_declares_is_rejected():
    # The never-modify-in-place guard — and what catches a second review stage reusing the
    # first's column names. `claim_id` is spec-identical to the source reviewed into it,
    # so rule 3 stays silent and only this rule can fire.
    with pytest.raises(ValidationError, match="already declares"):
        parse_stage(_stage_spec(
            queue={"reviewed_columns": {"assertion_text": "claim_id"}}))


def test_a_review_record_column_that_the_input_already_declares_is_rejected():
    # `decision` is declared `str` on output_schema, so rule 4 stays silent and only this
    # rule can fire.
    input_columns = _INPUT_COLUMNS + [{"name": "decision", "type": "str", "nullable": True}]
    with pytest.raises(ValidationError, match="already declares"):
        parse_stage(_stage_spec(input_columns=input_columns))


# ── 6. no added column name is used twice ────────────────────────────────────


def test_two_sources_mapping_to_the_same_target_are_rejected():
    # Both sources are `int`, like the `human_score` they collide on, so rule 3 stays
    # silent and only this rule can fire.
    with pytest.raises(ValidationError, match="named more than once"):
        parse_stage(_stage_spec(
            queue={"reviewed_columns": {"score": "human_score",
                                        "confidence": "human_score"}}))


def test_a_review_record_name_reused_as_a_reviewed_target_is_rejected():
    # The source is `str` non-null and `decision` is declared `str` on output_schema, so
    # rules 3 and 4 stay silent and only this rule can fire.
    with pytest.raises(ValidationError, match="named more than once"):
        parse_stage(_stage_spec(
            queue={"reviewed_columns": {"assertion_text": "decision"}}))


# ── the config's own shape ───────────────────────────────────────────────────


def test_an_empty_reviewed_columns_is_rejected():
    with pytest.raises(ValidationError, match="at least one column"):
        parse_stage(_stage_spec(queue={"reviewed_columns": {}}))
