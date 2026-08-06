"""What a join's signature may claim: the keys it reads from each side, and adds
that are exactly the columns `enrich_with` lands, each carrying its source's type.
A join only ever ADDS — every subject column flows through untouched."""
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
_KEY_READS = [
    {"input": "facilities",
     "columns": [{"name": "facility_id", "type": "str", "nullable": True}]},
    {"input": "filings",
     "columns": [{"name": "facility_id", "type": "str", "nullable": True}]},
]
_AMOUNT = {"name": "amount", "type": "int", "nullable": True}


def _join_stage(*, adds=None, enrich_with=None, left=_LEFT, right=_RIGHT,
                keys=None, stage_type="enrich"):
    return {
        "id": "add_filings",
        "description": "Enrich facilities with filings",
        "type": stage_type,
        "inputs": [
            {"id": "facilities", "schema": left},
            {"id": "filings", "schema": right},
        ],
        "join": {
            "keys": keys or [{"left": "facility_id", "right": "facility_id"}],
            "enrich_with": enrich_with or {"amount": "amount"},
        },
        "signature": {
            "form": "extends",
            "reads": _KEY_READS,
            "adds": [_AMOUNT] if adds is None else adds,
        },
    }


def _issues(stage_dict) -> str:
    with pytest.raises(ValidationError) as err:
        parse_stage(stage_dict)
    return str(err.value)


def test_enrich_with_source_not_producible_rejected():
    msg = _issues(_join_stage(
        enrich_with={"amount_typo": "amount_typo"},
        adds=[{"name": "amount_typo", "type": "int", "nullable": True}]))
    assert "amount_typo" in msg
    assert "join.enrich_with" in msg


def test_an_add_the_join_does_not_land_rejected():
    msg = _issues(_join_stage(adds=[{"name": "bogus", "type": "str", "nullable": True}]))
    assert "bogus" in msg and "join.enrich_with does not land" in msg


def test_landing_on_a_subject_column_is_a_refused_rewrite():
    # Landing the reference's `name` under the subject's own would rewrite that column.
    msg = _issues(_join_stage(
        enrich_with={"name": "name"},
        adds=[{"name": "name", "type": "int", "nullable": True}],
    ))
    assert "a join only ever ADDS" in msg


def test_a_landed_name_carries_its_sources_type():
    # Landed as an authored `name_r`, never a silent suffix — and it takes the SOURCE type.
    stage = parse_stage(_join_stage(
        enrich_with={"name": "name_r"},
        adds=[{"name": "name_r", "type": "int", "nullable": True}],
    ))
    assert stage.id == "add_filings"
    msg = _issues(_join_stage(
        enrich_with={"name": "name_r"},
        adds=[{"name": "name_r", "type": "str", "nullable": True}],
    ))
    assert "name_r" in msg and "int" in msg


def test_a_name_nothing_lands_as_is_not_an_add():
    # `name_r` exists only when an author lands a column there — nothing is
    # ever suffixed into it silently.
    msg = _issues(_join_stage(adds=[{"name": "name_r", "type": "int", "nullable": True}]))
    assert "name_r" in msg


def test_a_landed_column_the_signature_omits_rejected():
    msg = _issues(_join_stage(adds=[]))
    assert "amount" in msg and "the signature does not add it" in msg


def test_a_key_not_read_from_its_side_rejected():
    msg = _issues(_join_stage(
        adds=[_AMOUNT],
        keys=[{"left": "facility_id", "right": "facility_id"}],
    ) | {"signature": {"form": "extends", "reads": _KEY_READS[:1], "adds": [_AMOUNT]}})
    assert "join key .right" in msg and "reference input" in msg


def test_declared_type_mismatch_rejected():
    msg = _issues(_join_stage(adds=[{"name": "amount", "type": "str", "nullable": True}]))
    assert "amount" in msg and "int" in msg


@pytest.mark.parametrize("stage_type", ["enrich", "expand"])
def test_valid_join_passes(stage_type):
    stage = parse_stage(_join_stage(
        stage_type=stage_type,
        enrich_with={"amount": "amount", "kind": "kind"},
        adds=[_AMOUNT, {"name": "kind", "type": "str", "nullable": True}],
    ))
    assert stage.id == "add_filings"
    assert [c.name for c in stage.resolve_output_schema().columns] == [
        "facility_id", "name", "score", "amount", "kind"]
