"""Covers only the join-key check; `select` is validated separately, by
find_join_output_issues (see test_join_output_schema.py)."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, StageInput, JoinConfig
from app.models.stages.join import JoinStage, find_join_column_issues


def _enrich_stage(*, left_columns, right_columns, key_left, key_right, select=None):
    join: dict = {"keys": [{"left": key_left, "right": key_right}]}
    if select is not None:
        join["select"] = select
    return {
        "id": "j", "type": "enrich", "name": "j",
        "inputs": [
            {"id": "L", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in left_columns]}},
            {"id": "R", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in right_columns]}},
        ],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "join": join,
    }


def test_both_keys_present_ok():
    parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="b"))


def test_key_on_the_wrong_side_rejected():
    """`a` is declared on the LEFT edge and `b` on the RIGHT — a key that
    names them backwards (.left="b", .right="a") must be rejected on both
    sides, not silently matched by name across sides."""
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="b", key_right="a"))


def test_left_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="ghost", key_right="b"))


def test_right_key_missing_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="ghost"))


def test_select_referencing_absent_column_is_rejected_by_output_check():
    """The join stage's own config-column check never looked at `select` —
    but `select` naming a column the join can't produce IS rejected, by the
    separate output-schema check (find_join_output_issues)."""
    with pytest.raises(ValidationError):
        parse_stage(_enrich_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", select=["ghost"],
        ))


def test_find_join_column_issues_ignores_select():
    """The config-column check (find_join_column_issues) never inspects
    `select` — a select entry the join can't produce is rejected by the
    separate output-schema check (find_join_output_issues), not this one.
    Built via Stage.model_construct, bypassing Stage's own validators
    entirely, so the bad select never reaches the output-schema check that
    would otherwise reject the whole Stage at construction time (see
    test_select_referencing_absent_column_is_rejected_by_output_check
    above, which goes through that check instead)."""
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
            {"keys": [{"left": "a", "right": "b"}], "select": ["ghost"]}
        ),
    )
    assert find_join_column_issues(stage) == []

