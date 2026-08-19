from __future__ import annotations

import pytest
from conftest import reads_of, source_stage
from pydantic import ValidationError

from app.models import parse_stage, validate_workflow_draft

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
    edge = input_columns or _INPUT_COLUMNS
    # A review stage only ADDS and every input column flows, so `adds` is
    # whatever `output_columns` names beyond the edge — computed here so each
    # test can keep saying what the stage OUTPUTS.
    outputs = output_columns or _OUTPUT_COLUMNS
    flowing = {c["name"] for c in edge}
    return {
        "id": "wc", "type": "human_review_queue", "description": "wc",
        "inputs": [{"id": "src"}],
        "signature": {"form": "extends",
                      "reads": reads_of("src", edge),
                      "adds": [c for c in outputs if c["name"] not in flowing]},
        "queue": {**_QUEUE, **(queue or {})},
    }


def _issues(**kwargs) -> str:
    return "; ".join(_issue_list(**kwargs))


def _issue_list(*, spec=None, input_columns=None, **kwargs) -> list[str]:
    stage = spec if spec is not None else _stage_spec(input_columns=input_columns, **kwargs)
    return validate_workflow_draft([
        source_stage("src", input_columns or _INPUT_COLUMNS), stage,
    ])


# ── the valid config ────────────────────────────────────────────────────────


def test_a_fully_valid_config_reports_nothing():
    assert _issue_list() == []


# ── 1. the filter predicate ──────────────────────────────────────────────────


def test_filter_naming_a_column_the_input_lacks_is_rejected():
    assert "writer_confirmed" in _issues(queue={"filter": "writer_confirmed == True"})


def test_a_filter_over_input_columns_is_clean():
    assert _issue_list(queue={"filter": "assertion_text IS NOT NULL"}) == []


def test_a_filter_over_a_column_the_signature_does_not_read_is_rejected():
    # The filter would test a column the narrowed row does not carry.
    spec = _stage_spec(queue={"filter": "confidence > 3"})
    spec["signature"]["reads"] = reads_of(
        "src", [c for c in _INPUT_COLUMNS if c["name"] != "confidence"])
    assert "queue.filter tests `confidence`" in _issues(spec=spec)


# ── 1b. the declared review order ────────────────────────────────────────────


def test_a_sort_over_read_input_columns_is_clean():
    assert _issue_list(queue={"sort": [
        {"column": "score", "direction": "descending"},
        {"column": "claim_id", "direction": "ascending"},
    ]}) == []


def test_a_sort_naming_a_column_nothing_supplies_is_rejected():
    # The reads rule answers first: a column the input never had is not read either.
    assert "queue.sort orders by `income_usd`" in _issues(
        queue={"sort": [{"column": "income_usd", "direction": "descending"}]})


def test_a_sort_over_a_column_the_signature_does_not_read_is_rejected():
    # The queued row carries only the reads, so there would be nothing to sort on.
    spec = _stage_spec(queue={"sort": [{"column": "confidence", "direction": "ascending"}]})
    spec["signature"]["reads"] = reads_of(
        "src", [c for c in _INPUT_COLUMNS if c["name"] != "confidence"])
    assert "queue.sort orders by `confidence`" in _issues(spec=spec)


def test_a_sort_over_a_non_scalar_column_is_rejected():
    evidence = {"name": "evidence", "type": "json", "value_type": "str", "nullable": True}
    assert "has no order to put the queue in" in _issues(
        queue={"sort": [{"column": "evidence", "direction": "descending"}]},
        input_columns=_INPUT_COLUMNS + [evidence],
        output_columns=_OUTPUT_COLUMNS + [evidence],
    )


def test_the_same_column_named_twice_in_one_sort_is_rejected():
    assert "names column 'score' more than once" in _issues(queue={"sort": [
        {"column": "score", "direction": "descending"},
        {"column": "score", "direction": "ascending"},
    ]})


def test_a_sort_key_without_a_direction_is_rejected():
    # Which end of the queue the largest value goes is the whole decision.
    with pytest.raises(ValidationError, match="direction"):
        parse_stage(_stage_spec(queue={"sort": [{"column": "score"}]}))


# ── the read set ─────────────────────────────────────────────────────────────


def test_a_signature_reading_nothing_is_rejected():
    spec = _stage_spec()
    spec["signature"]["reads"] = []
    assert "reads nothing" in _issues(spec=spec)


def test_context_columns_must_exist_in_the_input_schema():
    assert "context_columns names column 'ghost'" in _issues(
        queue={"context_columns": ["ghost"]})


def test_context_columns_must_be_carried_by_the_signature():
    spec = _stage_spec(queue={"context_columns": ["confidence"]})
    spec["signature"]["reads"] = reads_of(
        "src", [c for c in _INPUT_COLUMNS if c["name"] != "confidence"])

    assert "context_columns names `confidence`" in _issues(spec=spec)


def test_a_reviewed_column_cannot_also_be_context():
    assert "cannot be both editable and context" in _issues(
        queue={"context_columns": ["score"]})


def test_a_context_column_cannot_be_named_twice():
    assert "names column 'claim_id' more than once" in _issues(
        queue={"context_columns": ["claim_id", "claim_id"]})


def test_an_empty_context_columns_list_is_clean():
    assert _issue_list(queue={"context_columns": []}) == []


# ── 2. reviewed source columns ───────────────────────────────────────────────


def test_a_reviewed_source_absent_from_the_input_is_rejected():
    assert "ghost" in _issues(queue={"reviewed_columns": {"ghost": "human_ghost"}})


def test_a_non_scalar_reviewed_source_is_rejected():
    """A `json` column cannot be answered through a form field, so it cannot be a reviewed source."""
    input_columns = _INPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str", "nullable": True},
    ]
    output_columns = _OUTPUT_COLUMNS + [
        {"name": "evidence", "type": "json", "value_type": "str", "nullable": True},
        {"name": "human_evidence", "type": "json", "value_type": "str", "nullable": True},
    ]
    assert "evidence" in _issues(
        queue={"reviewed_columns": {"score": "human_score", "evidence": "human_evidence"}},
        input_columns=input_columns, output_columns=output_columns)


# ── 3. reviewed target columns on the signature ──────────────────────────────


def test_a_reviewed_target_missing_from_the_signature_is_rejected():
    assert "human_verdict_score" in _issues(queue={"reviewed_columns": {"score": "human_verdict_score"}})


def test_a_reviewed_target_of_the_wrong_type_is_rejected():
    output_columns = [
        c if c["name"] != "human_score" else {"name": "human_score", "type": "str", "nullable": True}
        for c in _OUTPUT_COLUMNS
    ]
    assert "human_score" in _issues(output_columns=output_columns)


def test_a_reviewed_target_less_permissive_than_its_source_is_rejected():
    output_columns = [
        c if c["name"] != "human_score"
        else {"name": "human_score", "type": "int", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    assert "human_score" in _issues(output_columns=output_columns)


def test_a_reviewed_target_more_permissive_than_its_source_is_clean():
    input_columns = [
        c if c["name"] != "score" else {"name": "score", "type": "int", "nullable": False}
        for c in _INPUT_COLUMNS
    ]
    assert _issue_list(input_columns=input_columns) == []


# ── 4. the review-record columns on the signature ────────────────────────────


def test_a_review_record_column_missing_from_the_signature_is_rejected():
    assert "who_reviewed" in _issues(queue={"reviewer_column": "who_reviewed"})


def test_a_review_record_column_declared_non_str_is_rejected():
    output_columns = [
        c if c["name"] != "decision" else {"name": "decision", "type": "int", "nullable": True}
        for c in _OUTPUT_COLUMNS
    ]
    assert "decision" in _issues(output_columns=output_columns)


def test_a_declared_notes_column_must_be_added_by_the_signature():
    assert "review_notes" in _issues(queue={"review_notes_column": "review_notes"})


def test_a_declared_notes_column_present_on_the_signature_is_clean():
    assert _issue_list(queue={"review_notes_column": "review_notes"},
        output_columns=_OUTPUT_COLUMNS + [{"name": "review_notes", "type": "str", "nullable": True}]) == []


def test_a_non_nullable_review_record_column_is_rejected():
    # Reviewer is null on a filter-skipped row, so this would fail only at the END of a run.
    output_columns = [
        c if c["name"] != "reviewer_id"
        else {"name": "reviewer_id", "type": "str", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    assert "non-nullable" in _issues(output_columns=output_columns)


def test_a_non_nullable_verdict_column_is_clean_because_every_row_gets_one():
    output_columns = [
        c if c["name"] != "decision"
        else {"name": "decision", "type": "str", "nullable": False}
        for c in _OUTPUT_COLUMNS
    ]
    assert _issue_list(output_columns=output_columns) == []


# ── 5. no added column may collide with an input column ──────────────────────


def test_an_added_column_that_the_input_already_declares_is_rejected():
    assert "already declares" in _issues(queue={"reviewed_columns": {"assertion_text": "claim_id"}})


def test_a_review_record_column_that_the_input_already_declares_is_rejected():
    input_columns = _INPUT_COLUMNS + [{"name": "decision", "type": "str", "nullable": True}]
    assert "already declares" in _issues(input_columns=input_columns)


# ── 6. no added column name is used twice ────────────────────────────────────


def test_two_sources_mapping_to_the_same_target_are_rejected():
    assert "named more than once" in _issues(queue={
        "reviewed_columns": {"score": "human_score", "confidence": "human_score"}})


def test_a_review_record_name_reused_as_a_reviewed_target_is_rejected():
    assert "named more than once" in _issues(queue={"reviewed_columns": {"assertion_text": "decision"}})


# ── the config's own shape ───────────────────────────────────────────────────


def test_an_empty_reviewed_columns_is_rejected():
    assert "at least one column" in _issues(queue={"reviewed_columns": {}})
