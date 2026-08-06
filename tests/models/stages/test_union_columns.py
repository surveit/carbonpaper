from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "int", "nullable": True}]}


def _union_stage(*, input_schemas, produces=None):
    return {
        "id": "u", "type": "union", "description": "u",
        "inputs": [
            {"id": f"in{i}", "schema": schema} for i, schema in enumerate(input_schemas)
        ],
        "signature": {"form": "replaces",
                      "produces": (produces or input_schemas[0])["columns"]},
        "union": {},
    }


def test_matching_schemas_ok():
    parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]))


def test_three_matching_schemas_ok():
    parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA, _AB_SCHEMA]))


def test_mismatched_column_set_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}]}  # missing 'b'
    with pytest.raises(ValidationError, match=r"'b'"):
        parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, other], produces=_AB_SCHEMA))


def test_mismatched_column_type_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "str", "nullable": True}]}  # b: str not int
    with pytest.raises(ValidationError, match=r"'b'"):
        parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, other], produces=_AB_SCHEMA))


def test_mismatch_names_the_disagreeing_input():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}]}
    with pytest.raises(ValidationError, match="in1"):
        parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, other], produces=_AB_SCHEMA))


def test_produces_must_be_supplied_by_every_input():
    unsupplied = {"columns": [{"name": "a", "type": "str", "nullable": True},
                              {"name": "ghost", "type": "str", "nullable": True}]}
    with pytest.raises(ValidationError, match="ghost"):
        parse_stage(
            _union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA], produces=unsupplied)
        )


def test_needs_at_least_two_inputs():
    with pytest.raises(ValidationError):
        parse_stage(_union_stage(input_schemas=[_AB_SCHEMA]))


def test_round_trips():
    stage = parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]))
    dumped = stage.model_dump(by_alias=True)
    reloaded = parse_stage(dumped)
    assert reloaded.type == "union"
    assert reloaded.union is not None
