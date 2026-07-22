"""find_join_output_issues: a declared output_schema must be deliverable by the
join handle. Derivation mirrors pandas merge(..., suffixes=("", "_r")): a key
pair with the same name on both sides collapses to one column; every other
right column whose name collides with a left column appears as <name>_r;
`select`, when present, projects the merged columns and every entry must be
derivable."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import Stage

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
                keys=None):
    spec = {
        "id": "enrich",
        "name": "Join facilities to filings",
        "type": "join",
        "inputs": [
            {"id": "facilities", **({"schema": left} if left else {})},
            {"id": "filings", **({"schema": right} if right else {})},
        ],
        "join": {
            "type": "left",
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
        Stage.model_validate(stage_dict)
    return str(err.value)


def test_select_entry_not_derivable_rejected():
    # The runtime silently drops a select entry the merge lacks; save time
    # rejects it instead.
    msg = _issues(_join_stage(select=["facility_id", "amount_typo"]))
    assert "amount_typo" in msg


def test_declared_column_absent_from_merge_rejected():
    msg = _issues(_join_stage(
        output_columns=[{"name": "bogus", "type": "str"}],
    ))
    assert "bogus" in msg


def test_right_collision_reachable_only_as_suffixed():
    stage = Stage.model_validate(_join_stage(
        output_columns=[{"name": "name_r", "type": "int"}],
    ))
    assert stage.id == "enrich"
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
    # `score` survives the merge but select excludes it.
    msg = _issues(_join_stage(
        select=["facility_id", "amount"],
        output_columns=[{"name": "score", "type": "float"}],
    ))
    assert "score" in msg


def test_missing_either_edge_schema_skips():
    stage = Stage.model_validate(_join_stage(
        right=None,
        output_columns=[{"name": "anything_at_all", "type": "str"}],
    ))
    assert stage.id == "enrich"


def test_valid_join_passes():
    stage = Stage.model_validate(_join_stage(
        select=["facility_id", "name", "name_r", "amount"],
        output_columns=[
            {"name": "facility_id", "type": "str"},
            {"name": "name", "type": "str"},
            {"name": "name_r", "type": "int"},
            {"name": "amount", "type": "int"},
        ],
    ))
    assert stage.id == "enrich"
