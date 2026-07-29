from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}]}


def _union_stage(*, input_schemas, output_schema=None):
    return {
        "id": "u", "type": "union", "name": "u",
        "inputs": [
            {"id": f"in{i}", "schema": schema} for i, schema in enumerate(input_schemas)
        ],
        "output_schema": output_schema or input_schemas[0],
        "union": {},
    }


def test_matching_schemas_ok():
    Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]))


def test_three_matching_schemas_ok():
    Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA, _AB_SCHEMA]))


def test_mismatched_column_set_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str"}]}  # missing 'b'
    with pytest.raises(ValidationError, match=r"'b'"):
        Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, other], output_schema=_AB_SCHEMA))


def test_mismatched_column_type_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "str"}]}  # b: str not int
    with pytest.raises(ValidationError, match=r"'b'"):
        Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, other], output_schema=_AB_SCHEMA))


def test_mismatch_names_the_disagreeing_input():
    other = {"columns": [{"name": "a", "type": "str"}]}
    with pytest.raises(ValidationError, match="in1"):
        Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, other], output_schema=_AB_SCHEMA))


def test_output_schema_must_match_shared_input_schema():
    wrong_output = {"columns": [{"name": "a", "type": "str"}]}
    with pytest.raises(ValidationError, match="output_schema"):
        Stage.model_validate(
            _union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA], output_schema=wrong_output)
        )


def test_needs_at_least_two_inputs():
    with pytest.raises(ValidationError):
        Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA]))


def test_round_trips():
    stage = Stage.model_validate(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]))
    dumped = stage.model_dump(by_alias=True)
    reloaded = Stage.model_validate(dumped)
    assert reloaded.type == "union"
    assert reloaded.union is not None
