"""Covers the join config-column checks: every key must resolve against its
side's upstream, every `enrich_with` source must exist on the reference, and every
landed name must be new to the subject — a join adds, never rewrites."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, validate_workflow_draft
from app.models.stages.join import JoinConfig, JoinStage, find_join_column_issues
from app.models.workflow_stage import WorkflowStageInput
from app.models.schema import TableSchema
from conftest import source_stage


def _workflow(*, left_columns, right_columns, key_left, key_right, enrich_with):
    return [
        source_stage("subject", [_col(c) for c in left_columns]),
        source_stage("reference", [_col(c) for c in right_columns]),
        _enrich_stage(
            left_columns=left_columns, right_columns=right_columns,
            key_left=key_left, key_right=key_right, enrich_with=enrich_with,
        ),
    ]


def _issues(**kwargs):
    return "; ".join(validate_workflow_draft(_workflow(**kwargs)))


def _enrich_stage(*, left_columns, right_columns, key_left, key_right, enrich_with):
    return {
        "id": "j", "type": "enrich", "description": "j",
        "inputs": [{"id": "subject"}, {"id": "reference"}],
        "join": {"keys": [{"left": key_left, "right": key_right}], "enrich_with": enrich_with},
        # These tests vary the keys and the landed columns to exercise the CONFIG
        # checks, so the signature is computed from that config rather than pinned:
        # a hand-written one would fail its own cross-check first and mask them.
        "signature": {
            "form": "extends",
            "reads": [
                entry for entry in (
                    {"input": "subject", "columns": [_col(key_left)] if key_left in left_columns else []},
                    {"input": "reference", "columns": [_col(key_right)] if key_right in right_columns else []},
                ) if entry["columns"]
            ],
            "adds": [_col(landed) for landed in enrich_with.values()],
        },
    }


def _col(name):
    return {"name": name, "type": "str", "nullable": False}


def test_both_keys_present_ok():
    assert _issues(
        left_columns=["a"], right_columns=["b"], key_left="a", key_right="b",
        enrich_with={"b": "b"}) == ""


def test_key_on_the_wrong_side_rejected():
    assert _issues(
        left_columns=["a"], right_columns=["b"], key_left="b", key_right="a",
        enrich_with={"b": "b"})


def test_left_key_missing_rejected():
    assert _issues(
        left_columns=["a"], right_columns=["b"], key_left="ghost", key_right="b",
        enrich_with={"b": "b"})


def test_right_key_missing_rejected():
    assert _issues(
        left_columns=["a"], right_columns=["b"], key_left="a", key_right="ghost",
        enrich_with={"b": "b"})


def test_enrich_with_source_absent_from_the_reference_rejected():
    assert "join.enrich_with" in _issues(
        left_columns=["a"], right_columns=["b"], key_left="a", key_right="b",
        enrich_with={"ghost": "ghost"})


def test_landing_on_a_subject_column_rejected_as_a_rewrite():
    assert "a join only ever ADDS" in _issues(
        left_columns=["a", "dup"], right_columns=["b", "dup"], key_left="a", key_right="b",
        enrich_with={"dup": "dup"})


def test_the_out_is_landing_the_same_source_under_a_new_name():
    assert _issues(
        left_columns=["a", "dup"], right_columns=["b", "dup"], key_left="a", key_right="b",
        enrich_with={"dup": "dup_r"}) == ""


def test_two_sources_landing_as_one_name_rejected():
    # Refused by JoinConfig itself, which needs no upstream to see it.
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b", "c"], key_left="a", key_right="b",
            enrich_with={"b": "same", "c": "same"},
        ))
    assert "lands two columns as" in str(err.value)


def test_landing_onto_a_right_key_name_rejected():
    assert "join key on the reference side" in _issues(
        left_columns=["a"], right_columns=["b", "z"], key_left="a", key_right="b",
        enrich_with={"z": "b"})


def test_landing_inside_the_internal_namespace_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b",
            enrich_with={"b": "_b"},
        ))
    assert "reserved" in str(err.value)


def test_find_join_column_issues_reports_enrich_with():
    stage = JoinStage.model_construct(
        id="j",
        name="j",
        type="enrich",
        join=JoinConfig.model_validate(
            {"keys": [{"left": "a", "right": "b"}], "enrich_with": {"ghost": "ghost"}}
        ),
    )
    inputs = [
        WorkflowStageInput(id="subject", table_schema=TableSchema(columns=[_col("a")])),
        WorkflowStageInput(id="reference", table_schema=TableSchema(columns=[_col("b")])),
    ]
    issues = find_join_column_issues(stage, inputs)
    assert len(issues) == 1 and "join.enrich_with" in issues[0] and "'ghost'" in issues[0]
