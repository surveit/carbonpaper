from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage
from app.models.stages.human_review_queue import find_queue_column_issues

_INPUT_COLUMNS = [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "assertion_text", "type": "str", "nullable": False},
    {"name": "score", "type": "int"},
]
_OUTPUT_COLUMNS = [
    {"name": "claim_id", "type": "str", "nullable": False},
    {"name": "assertion_text", "type": "str", "nullable": False},
    {"name": "score", "type": "int"},
    {"name": "human_score", "type": "int"},
    {"name": "decision", "type": "str"},
    {"name": "reviewer_id", "type": "str"},
    {"name": "reviewed_at", "type": "str"},
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


# ── the valid config, and the cases with nothing to resolve against ──────────


def test_a_fully_valid_config_reports_nothing():
    stage = Stage.model_validate(_stage_spec())
    assert find_queue_column_issues(stage) == []


# ── 1. the filter predicate ──────────────────────────────────────────────────


def test_filter_naming_a_column_the_input_lacks_is_rejected():
    with pytest.raises(ValidationError, match="writer_confirmed"):
        Stage.model_validate(_stage_spec(queue={"filter": "writer_confirmed == True"}))


def test_a_filter_over_input_columns_is_clean():
    Stage.model_validate(_stage_spec(queue={"filter": "assertion_text IS NOT NULL"}))


# ── 2. reviewed source columns ───────────────────────────────────────────────


def test_a_reviewed_source_absent_from_the_input_is_rejected():
    with pytest.raises(ValidationError, match="ghost"):
        Stage.model_validate(_stage_spec(queue={"reviewed_columns": {"ghost": "human_ghost"}}))


def test_a_non_scalar_reviewed_source_is_rejected():
    """A `json` column cannot be answered through a form field, so it cannot be
    a reviewed source."""
    input_columns = _INPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str"},
    ]
    output_columns = _OUTPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str"},
        {"name": "human_evidence", "type": "json", "value_type": "str"},
    ]
    with pytest.raises(ValidationError, match="evidence"):
        Stage.model_validate(_stage_spec(
            queue={"reviewed_columns": {"score": "human_score", "evidence": "human_evidence"}},
            input_columns=input_columns, output_columns=output_columns,
        ))


# ── 3. reviewed target columns on output_schema ──────────────────────────────


def test_a_reviewed_target_missing_from_output_schema_is_rejected():
    with pytest.raises(ValidationError, match="human_verdict_score"):
        Stage.model_validate(_stage_spec(
            queue={"reviewed_columns": {"score": "human_verdict_score"}}))


def test_a_reviewed_target_of_the_wrong_type_is_rejected():
    output_columns = [
        c if c["name"] != "human_score" else {"name": "human_score", "type": "str"}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="human_score"):
        Stage.model_validate(_stage_spec(output_columns=output_columns))


def test_a_reviewed_target_less_permissive_than_its_source_is_rejected():
    """The source may be null, so a non-nullable target could not hold it."""
    output_columns = [
        c if c["name"] != "human_score"
        else {"name": "human_score", "type": "int", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="human_score"):
        Stage.model_validate(_stage_spec(output_columns=output_columns))


def test_a_reviewed_target_more_permissive_than_its_source_is_clean():
    input_columns = [
        c if c["name"] != "score" else {"name": "score", "type": "int", "nullable": False}
        for c in _INPUT_COLUMNS
    ]
    Stage.model_validate(_stage_spec(input_columns=input_columns))


# ── 4. the bookkeeping columns on output_schema ──────────────────────────────


def test_a_bookkeeping_column_missing_from_output_schema_is_rejected():
    with pytest.raises(ValidationError, match="who_reviewed"):
        Stage.model_validate(_stage_spec(queue={"reviewer_column": "who_reviewed"}))


def test_a_bookkeeping_column_declared_non_str_is_rejected():
    output_columns = [
        c if c["name"] != "decision" else {"name": "decision", "type": "int"}
        for c in _OUTPUT_COLUMNS
    ]
    with pytest.raises(ValidationError, match="decision"):
        Stage.model_validate(_stage_spec(output_columns=output_columns))


def test_a_declared_notes_column_must_be_declared_on_output_schema():
    with pytest.raises(ValidationError, match="review_notes"):
        Stage.model_validate(_stage_spec(queue={"review_notes_column": "review_notes"}))


def test_a_declared_notes_column_present_on_output_schema_is_clean():
    Stage.model_validate(_stage_spec(
        queue={"review_notes_column": "review_notes"},
        output_columns=_OUTPUT_COLUMNS + [{"name": "review_notes", "type": "str"}],
    ))


# ── 5. no added column may collide with an input column ──────────────────────


def test_an_added_column_that_the_input_already_declares_is_rejected():
    """The never-modify-in-place guard — and what catches a second review stage
    reusing the first's column names."""
    with pytest.raises(ValidationError, match="assertion_text"):
        Stage.model_validate(_stage_spec(
            queue={"reviewed_columns": {"score": "assertion_text"}}))


def test_a_bookkeeping_column_that_the_input_already_declares_is_rejected():
    input_columns = _INPUT_COLUMNS + [{"name": "decision", "type": "str"}]
    with pytest.raises(ValidationError, match="decision"):
        Stage.model_validate(_stage_spec(input_columns=input_columns))


# ── 6. no added column name is used twice ────────────────────────────────────


def test_two_sources_mapping_to_the_same_target_are_rejected():
    with pytest.raises(ValidationError, match="human_score"):
        Stage.model_validate(_stage_spec(
            queue={"reviewed_columns": {"score": "human_score",
                                        "assertion_text": "human_score"}}))


def test_a_bookkeeping_name_reused_as_a_reviewed_target_is_rejected():
    with pytest.raises(ValidationError, match="decision"):
        Stage.model_validate(_stage_spec(
            queue={"reviewed_columns": {"score": "decision"}}))


# ── the config's own shape ───────────────────────────────────────────────────


def test_an_empty_reviewed_columns_is_rejected():
    with pytest.raises(ValidationError, match="at least one column"):
        Stage.model_validate(_stage_spec(queue={"reviewed_columns": {}}))
