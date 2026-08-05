"""Covers the join config-column checks: every key must resolve against its
side's edge, every `enrich_with` source must exist on the reference, and every landed
name must be new to the subject — a join adds, never rewrites."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, StageInput, JoinConfig
from app.models.stages.join import JoinStage, find_join_column_issues


def _enrich_stage(*, left_columns, right_columns, key_left, key_right, enrich_with):
    return {
        "id": "j", "type": "enrich", "name": "j",
        "inputs": [
            {"id": "L", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in left_columns]}},
            {"id": "R", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in right_columns]}},
        ],
        "join": {"keys": [{"left": key_left, "right": key_right}], "enrich_with": enrich_with},
        # These tests vary the keys and the landed columns to exercise the CONFIG
        # checks, so the signature is computed from that config rather than pinned:
        # a hand-written one would fail its own cross-check first and mask them.
        "signature": {
            "form": "extends",
            "reads": [
                entry for entry in (
                    {"input": "L", "columns": [_col(key_left)] if key_left in left_columns else []},
                    {"input": "R", "columns": [_col(key_right)] if key_right in right_columns else []},
                ) if entry["columns"]
            ],
            "adds": [_col(landed) for landed in enrich_with.values()],
        },
    }


def _col(name):
    return {"name": name, "type": "str", "nullable": False}


def test_both_keys_present_ok():
    parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", enrich_with={"b": "b"}))


def test_key_on_the_wrong_side_rejected():
    """`a` is declared on the LEFT edge and `b` on the RIGHT — a key that
    names them backwards (.left="b", .right="a") must be rejected on both
    sides, not silently matched by name across sides."""
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="b", key_right="a", enrich_with={"b": "b"}))


def test_left_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="ghost", key_right="b", enrich_with={"b": "b"}))


def test_right_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="ghost", enrich_with={"b": "b"}))


def test_enrich_with_source_absent_from_the_reference_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", enrich_with={"ghost": "ghost"},
        ))
    assert "join.enrich_with" in str(err.value)


def test_landing_on_a_subject_column_rejected_as_a_rewrite():
    """Landing on a name the subject carries would rewrite it — a join only adds."""
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a", "dup"], right_columns=["b", "dup"], key_left="a", key_right="b",
            enrich_with={"dup": "dup"},
        ))
    assert "a join only ever ADDS" in str(err.value)


def test_the_out_is_landing_the_same_source_under_a_new_name():
    parse_stage(_enrich_stage(
        left_columns=["a", "dup"], right_columns=["b", "dup"], key_left="a", key_right="b",
        enrich_with={"dup": "dup_r"},
    ))


def test_two_sources_landing_as_one_name_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b", "c"], key_left="a", key_right="b",
            enrich_with={"b": "same", "c": "same"},
        ))
    assert "lands two columns as" in str(err.value)


def test_landing_onto_a_right_key_name_rejected():
    """Landing onto the reference-side key would corrupt the key the merge reads."""
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b", "z"], key_left="a", key_right="b",
            enrich_with={"z": "b"},
        ))
    assert "join key on the reference side" in str(err.value)


def test_landing_inside_the_internal_namespace_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b",
            enrich_with={"b": "_b"},
        ))
    assert "reserved" in str(err.value)


def test_find_join_column_issues_reports_enrich_with():
    """Observed from the check directly (model_construct bypasses Stage's validators)."""
    stage = JoinStage.model_construct(
        id="j",
        name="j",
        type="enrich",
        inputs=[
            StageInput.model_validate(
                {"id": "L", "schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]}}
            ),
            StageInput.model_validate(
                {"id": "R", "schema": {"columns": [{"name": "b", "type": "str", "nullable": False}]}}
            ),
        ],
        join=JoinConfig.model_validate(
            {"keys": [{"left": "a", "right": "b"}], "enrich_with": {"ghost": "ghost"}}
        ),
    )
    issues = find_join_column_issues(stage)
    assert len(issues) == 1 and "join.enrich_with" in issues[0] and "'ghost'" in issues[0]
