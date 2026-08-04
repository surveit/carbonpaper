from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models.stage import parse_stage

_LEFT = {
    "columns": [
        {"name": "facility_id", "type": "str", "nullable": True},
        {"name": "name", "type": "str", "nullable": True},
        {"name": "score", "type": "float", "nullable": True},
    ],
}
_RIGHT = {
    "columns": [
        {"name": "facility_id", "type": "str", "nullable": True},
        {"name": "name", "type": "int", "nullable": True},
        {"name": "amount", "type": "int", "nullable": True},
        {"name": "kind", "type": "str", "nullable": True},
    ],
}


def _join_stage(*, output_columns=None, bring=None, left=_LEFT, right=_RIGHT,
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
            "bring": bring or ["amount"],
        },
    }
    if output_columns is not None:
        spec["output_schema"] = {"columns": output_columns}
    return spec


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def test_bring_entry_not_producible_rejected():
    msg = _issues(_join_stage(
        bring=["amount_typo"],
        output_columns=[{"name": "facility_id", "type": "str", "nullable": True}]))
    assert "amount_typo" in msg
    assert "join.bring" in msg


def test_declared_column_absent_from_join_rejected():
    msg = _issues(_join_stage(
        output_columns=[{"name": "bogus", "type": "str", "nullable": True}],
    ))
    assert "bogus" in msg


def test_bring_colliding_with_subject_refused_never_renamed():
    # The reference's own `name` (int) collides with the subject's `name`
    # (str); under the old handle it arrived as `name_r` — now the bring
    # itself is refused.
    msg = _issues(_join_stage(
        bring=["name"],
        output_columns=[{"name": "facility_id", "type": "str", "nullable": True}],
    ))
    assert "refused, never renamed" in msg


def test_suffixed_name_is_not_producible():
    # No brought column is ever renamed, so the old `<name>_r` spelling names
    # nothing the join can deliver.
    msg = _issues(_join_stage(
        output_columns=[{"name": "name_r", "type": "int", "nullable": True}],
    ))
    assert "name_r" in msg


def test_subject_column_keeps_its_own_type():
    msg = _issues(_join_stage(
        output_columns=[{"name": "name", "type": "int", "nullable": True}],
    ))
    assert "'name'" in msg and "str" in msg


def test_same_name_key_collapses():
    msg = _issues(_join_stage(
        output_columns=[{"name": "facility_id_r", "type": "str", "nullable": True}],
    ))
    assert "facility_id_r" in msg


def test_declared_type_mismatch_rejected():
    msg = _issues(_join_stage(
        output_columns=[{"name": "amount", "type": "str", "nullable": True}],
    ))
    assert "amount" in msg and "int" in msg


def test_un_brought_reference_column_not_producible():
    # `kind` sits on the reference edge but bring does not name it, so the
    # declared output cannot carry it.
    msg = _issues(_join_stage(
        bring=["amount"],
        output_columns=[{"name": "kind", "type": "str", "nullable": True}],
    ))
    assert "kind" in msg


@pytest.mark.parametrize("stage_type", ["enrich", "expand"])
def test_valid_join_passes(stage_type):
    stage = parse_stage(_join_stage(
        stage_type=stage_type,
        bring=["amount", "kind"],
        output_columns=[
            {"name": "facility_id", "type": "str", "nullable": True},
            {"name": "name", "type": "str", "nullable": True},
            {"name": "amount", "type": "int", "nullable": True},
            {"name": "kind", "type": "str", "nullable": True},
        ],
    ))
    assert stage.id == "add_filings"
