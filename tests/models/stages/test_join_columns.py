"""Tests for the join stage's config-column check (app/models/stages/
join.py, wired into Stage._config_columns_resolve): each key's `.left` must
resolve against input 0's edge schema and `.right` against input 1's. The
config-column check itself has no `select`/`_r`-suffix check — but `select`
IS validated separately, by the output-schema check (find_join_output_issues,
see test_join_output_schema.py); this file covers only the join-key check."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage


def _join_stage(*, left_columns, right_columns, key_left, key_right, select=None):
    join: dict = {"type": "inner", "keys": [{"left": key_left, "right": key_right}]}
    if select is not None:
        join["select"] = select
    return {
        "id": "j", "type": "join", "name": "j",
        "inputs": [
            {"id": "L", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in left_columns]}},
            {"id": "R", "schema": {"columns": [{"name": c, "type": "str", "nullable": False} for c in right_columns]}},
        ],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "join": join,
    }


def test_both_keys_present_ok():
    Stage.model_validate(_join_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="b"))


def test_key_on_the_wrong_side_rejected():
    """`a` is declared on the LEFT edge and `b` on the RIGHT — a key that
    names them backwards (.left="b", .right="a") must be rejected on both
    sides, not silently matched by name across sides."""
    with pytest.raises(ValidationError):
        Stage.model_validate(_join_stage(left_columns=["a"], right_columns=["b"], key_left="b", key_right="a"))


def test_left_key_missing_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_join_stage(left_columns=["a"], right_columns=["b"], key_left="ghost", key_right="b"))


def test_right_key_missing_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_join_stage(left_columns=["a"], right_columns=["b"], key_left="a", key_right="ghost"))


def test_select_referencing_absent_column_is_rejected_by_output_check():
    """The join stage's own config-column check never looked at `select` —
    but `select` naming a column the merge can't produce IS rejected, by the
    separate output-schema check (find_join_output_issues)."""
    with pytest.raises(ValidationError):
        Stage.model_validate(_join_stage(
            left_columns=["a"], right_columns=["b"], key_left="a", key_right="b", select=["ghost"],
        ))


def test_side_with_no_edge_schema_is_skipped():
    stage = {
        "id": "j", "type": "join", "name": "j",
        "inputs": ["L", "R"],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": False}]},
        "join": {"type": "inner", "keys": [{"left": "ghost_left", "right": "ghost_right"}]},
    }
    Stage.model_validate(stage)
