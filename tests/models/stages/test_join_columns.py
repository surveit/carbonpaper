"""Covers the join config-column checks: every key must resolve against its
side's edge, and every `bring` entry must name a reference column the subject
does not already carry."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, StageInput, JoinConfig
from app.models.stages.join import JoinStage, find_join_column_issues


def _enrich_stage(*, left_columns, right_columns, key_left, key_right, bring):
    return {
        "id": "j", "type": "enrich", "name": "j",
        "inputs": [
            {"id": "L", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in left_columns]}},
            {"id": "R", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in right_columns]}},
        ],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "join": {"keys": [{"left": key_left, "right": key_right}], "bring": bring},
    }


def test_both_keys_present_ok():
    parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", bring=["b"]))


def test_key_on_the_wrong_side_rejected():
    """`a` is declared on the LEFT edge and `b` on the RIGHT — a key that
    names them backwards (.left="b", .right="a") must be rejected on both
    sides, not silently matched by name across sides."""
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="b", key_right="a", bring=["b"]))


def test_left_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="ghost", key_right="b", bring=["b"]))


def test_right_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="ghost", bring=["b"]))


def test_bring_referencing_absent_column_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", bring=["ghost"],
        ))
    assert "join.bring" in str(err.value)


def test_bring_colliding_with_subject_rejected():
    """A brought column the subject already carries is refused, never renamed."""
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a", "dup"], right_columns=["b", "dup"], key_left="a", key_right="b", bring=["dup"],
        ))
    assert "refused, never renamed" in str(err.value)


def test_duplicate_bring_entries_rejected():
    with pytest.raises(ValidationError) as err:
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", bring=["b", "b"],
        ))
    assert "duplicate" in str(err.value)


def test_find_join_column_issues_reports_bring():
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
            {"keys": [{"left": "a", "right": "b"}], "bring": ["ghost"]}
        ),
    )
    issues = find_join_column_issues(stage)
    assert len(issues) == 1 and "join.bring" in issues[0] and "'ghost'" in issues[0]
