from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage

_LEFT = {
    "columns": [
        {"name": "facility_id", "type": "str"},
        {"name": "name", "type": "str"},
        {"name": "score", "type": "float"},
    ],
}
_RIGHT = {
    "columns": [
        {"name": "facility_id", "type": "str"},
        {"name": "name", "type": "int"},
        {"name": "amount", "type": "int"},
    ],
}


def _join_stage(*, output_columns=None, select=None, left=_LEFT, right=_RIGHT,
                keys=None, stage_type="enrich"):
    spec = {
        "id": "add_filings",
        "name": "Enrich facilities with filings",
        "type": stage_type,
        "inputs": [
            {"id": "facilities", "schema": left},
            {"id": "filings", "schema": right},
        ],
        "join": {
            "keys": keys or [{"left": "facility_id", "right": "facility_id"}],
        },
    }
    if select is not None:
        spec["join"]["select"] = select
    if output_columns is not None:
        spec["output_schema"] = {"columns": output_columns}
    return spec


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def test_select_entry_not_producible_rejected():
    # The runtime silently drops a select entry the join lacks; save time
    # rejects it instead.
    msg = _issues(_join_stage(
        select=["facility_id", "amount_typo"],
        output_columns=[{"name": "facility_id", "type": "str"}]))
    assert "amount_typo" in msg
    assert "join.select" in msg


def test_declared_column_absent_from_join_rejected():
    msg = _issues(_join_stage(
        output_columns=[{"name": "bogus", "type": "str"}],
    ))
    assert "bogus" in msg


def test_right_collision_reachable_only_as_suffixed():
    stage = parse_stage(_join_stage(
        output_columns=[{"name": "name_r", "type": "int"}],
    ))
    assert stage.id == "add_filings"
    msg = _issues(_join_stage(
        output_columns=[{"name": "name_r", "type": "str"}],
    ))
    assert "name_r" in msg and "int" in msg


def test_bare_collision_name_takes_left_type():
    msg = _issues(_join_stage(
        output_columns=[{"name": "name", "type": "int"}],
    ))
    assert "'name'" in msg and "str" in msg


def test_same_name_key_collapses():
    msg = _issues(_join_stage(
        output_columns=[{"name": "facility_id_r", "type": "str"}],
    ))
    assert "facility_id_r" in msg


def test_declared_type_mismatch_rejected():
    msg = _issues(_join_stage(
        output_columns=[{"name": "amount", "type": "str"}],
    ))
    assert "amount" in msg and "int" in msg


def test_select_projection_limits_declared():
    # `score` survives the join but select excludes it.
    msg = _issues(_join_stage(
        select=["facility_id", "amount"],
        output_columns=[{"name": "score", "type": "float"}],
    ))
    assert "score" in msg



@pytest.mark.parametrize("stage_type", ["enrich", "expand"])
def test_valid_join_passes(stage_type):
    stage = parse_stage(_join_stage(
        stage_type=stage_type,
        select=["facility_id", "name", "name_r", "amount"],
        output_columns=[
            {"name": "facility_id", "type": "str"},
            {"name": "name", "type": "str"},
            {"name": "name_r", "type": "int"},
            {"name": "amount", "type": "int"},
        ],
    ))
    assert stage.id == "add_filings"
