from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"}]}


def _filter_stage(*, output_schema=None, filter_cfg=None):
    return {
        "id": "f", "type": "filter_rows", "name": "f",
        "inputs": [{"id": "src", "schema": _AB_SCHEMA}],
        "output_schema": output_schema or _AB_SCHEMA,
        "filter": filter_cfg or {
            "kind": "inline",
            "code": "def should_include(row): return row['b'] > 0",
        },
    }


def test_matching_output_schema_ok():
    Stage.model_validate(_filter_stage())


def test_output_schema_must_equal_input_schema():
    wrong_output = {"columns": [{"name": "a", "type": "str"}]}
    with pytest.raises(ValidationError, match="output_schema"):
        Stage.model_validate(_filter_stage(output_schema=wrong_output))


def test_output_schema_extra_column_rejected():
    extra = {"columns": [{"name": "a", "type": "str"}, {"name": "b", "type": "int"},
                          {"name": "c", "type": "str"}]}
    with pytest.raises(ValidationError, match="output_schema"):
        Stage.model_validate(_filter_stage(output_schema=extra))


def test_inline_code_must_define_should_include():
    with pytest.raises(ValidationError, match="should_include"):
        Stage.model_validate(_filter_stage(filter_cfg={
            "kind": "inline", "code": "def other(row): return True",
        }))


def test_takes_exactly_one_input():
    stage = _filter_stage()
    stage["inputs"].append({"id": "src2", "schema": _AB_SCHEMA})
    with pytest.raises(ValidationError):
        Stage.model_validate(stage)


def test_round_trips():
    stage = Stage.model_validate(_filter_stage())
    dumped = stage.model_dump(by_alias=True)
    reloaded = Stage.model_validate(dumped)
    assert reloaded.type == "filter_rows"
    assert reloaded.filter is not None
    assert reloaded.filter.code == _filter_stage()["filter"]["code"]
