"""Tests for app/models/stages/inner.py — the inner half of a 1:1 stage's schema
contract: the derived read of what a stage ADDS, and the declared read-set."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app import models as m

_IN_SCHEMA = {
    "columns": [
        {"name": "id", "type": "str"},
        {"name": "notes", "type": "str"},
        {"name": "untouched", "type": "str"},
    ],
    "primary_key": ["id"],
}
_OUT_SCHEMA = {
    "columns": [
        *_IN_SCHEMA["columns"],
        {"name": "topic", "type": "str", "nullable": True},
    ],
    "primary_key": ["id"],
}


def _row_fn(**kw):
    """A python_row_function over _IN_SCHEMA that adds `topic`."""
    return {
        "id": "tag", "name": "Tag", "type": "python_row_function",
        "inputs": [{"id": "up", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
        **kw,
    }


# ── the derived half: what a stage adds ──────────────────────────────────────
def test_inner_adds_is_the_output_minus_the_input():
    """Derived, not declared — so it answers for a stage that has never heard of
    `inner`, which is every stage authored before it existed."""
    stage = m.Stage.model_validate(_row_fn())
    assert [c.name for c in stage.inner_adds().columns] == ["topic"]


def test_inner_adds_answers_for_every_one_to_one_type():
    """The subtraction is a fact about the stage's GRAIN, not about its handle, so
    it answers for a python_row_function and an llm_transform alike — neither can
    reshape its input, so in both cases output − input is exactly what it adds."""
    row_fn = m.Stage.model_validate(_row_fn())
    llm = m.Stage.model_validate({
        "id": "classify", "name": "Classify", "type": "llm_transform",
        "inputs": [{"id": "up", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "llm": {"model": "opus", "prompt_instructions": "Pick a topic.",
                "prompt_data_template": "{notes}"},
    })
    assert [c.name for c in row_fn.inner_adds().columns] == ["topic"]
    assert [c.name for c in llm.inner_adds().columns] == ["topic"]


def test_inner_adds_is_none_for_a_frame_function():
    """A frame function may drop or reorder columns, so its output_schema is
    authoritative and there is no inner/outer split to speak of."""
    stage = m.Stage.model_validate({
        "id": "rank", "name": "Rank", "type": "python_frame_function",
        "inputs": [{"id": "up", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
    })
    assert stage.inner_adds() is None


# ── the declared half: reads ─────────────────────────────────────────────────
def test_reads_defaults_to_none_meaning_every_column():
    """An absent read-set is 'shows the whole row', which a caller must not
    confuse with the empty set."""
    assert m.Stage.model_validate(_row_fn()).inner_reads() is None


def test_declared_reads_round_trips():
    stage = m.Stage.model_validate(_row_fn(inner={"reads": ["notes"]}))
    assert stage.inner_reads() == ["notes"]


def test_reads_an_undeclared_column_is_rejected():
    with pytest.raises(ValidationError, match="which its input 'up' does not declare"):
        m.Stage.model_validate(_row_fn(inner={"reads": ["nope"]}))


def test_reads_a_column_this_stage_adds_is_rejected():
    """`topic` is on the output, not the input — reading it would be reading this
    stage's own result, and naming it means inner and outer got confused."""
    with pytest.raises(ValidationError, match="cannot read its own output"):
        m.Stage.model_validate(_row_fn(inner={"reads": ["topic"]}))


def test_inner_is_rejected_on_a_stage_that_is_not_one_to_one():
    with pytest.raises(ValidationError, match="describes a 1:1 row transform"):
        m.Stage.model_validate({
            "id": "rank", "name": "Rank", "type": "python_frame_function",
            "inputs": [{"id": "up", "schema": _IN_SCHEMA}],
            "output_schema": _OUT_SCHEMA,
            "function": {"kind": "inline", "code": "def transform(df):\n    return df"},
            "inner": {"reads": ["notes"]},
        })


def test_an_llm_transform_can_declare_reads_too():
    """`inner` is not python-specific: the concept is the stage's grain, so every
    grain-and-order-preserving type carries it."""
    stage = m.Stage.model_validate({
        "id": "classify", "name": "Classify", "type": "llm_transform",
        "inputs": [{"id": "up", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "llm": {
            "model": "opus",
            "prompt_instructions": "Pick a topic.",
            "prompt_data_template": "{notes}",
        },
        "inner": {"reads": ["notes"]},
    })
    assert stage.inner_reads() == ["notes"]
    assert [c.name for c in stage.inner_adds().columns] == ["topic"]


# ── narrowed test-case rows ──────────────────────────────────────────────────
def test_no_read_set_means_no_narrowing():
    """Without a declared read-set there is nothing to narrow to, so a case still
    instances the whole row."""
    from app.models.stages.inner import scoped_row_schemas
    assert scoped_row_schemas(m.Stage.model_validate(_row_fn())) is None


def test_a_read_set_narrows_a_case_to_what_the_stage_reads():
    """The point of the read-set on the review surface: a case for a stage that
    reads 1 of 3 columns is a 1-column row in and a 2-column row out, not 3 and 4."""
    from app.models.stages.inner import scoped_row_schemas
    stage = m.Stage.model_validate(_row_fn(inner={"reads": ["notes"]}))
    input_row, expected_row = scoped_row_schemas(stage)
    assert [c.name for c in input_row.columns] == ["notes"]
    assert [c.name for c in expected_row.columns] == ["notes", "topic"]


def test_narrowed_rows_carry_no_primary_key():
    """They describe one row's shape, not a table — and the read-set need not
    include the key, which would otherwise make the schema inconsistent."""
    from app.models.stages.inner import scoped_row_schemas
    stage = m.Stage.model_validate(_row_fn(inner={"reads": ["notes"]}))
    assert all(schema.primary_key is None for schema in scoped_row_schemas(stage))


def test_an_empty_read_set_narrows_to_the_added_columns_only():
    """A stage that reads nothing (a constant column, a row counter) is legal, and
    its cases assert only what it adds."""
    from app.models.stages.inner import scoped_row_schemas
    stage = m.Stage.model_validate(_row_fn(inner={"reads": []}))
    input_row, expected_row = scoped_row_schemas(stage)
    assert [c.name for c in input_row.columns] == []
    assert [c.name for c in expected_row.columns] == ["topic"]
