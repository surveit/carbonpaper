from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import parse_stage, validate_workflow_draft
from app.models.schema import TableSchema
from app.models.stages.signature import promised_output_schema
from app.models.workflow import parse_workflow
from app.models.workflow_stage import WorkflowStageInput
from conftest import source_stage

_AB_SCHEMA = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "int", "nullable": True}]}


def _union_stage(*, input_schemas):
    return {
        "id": "u", "type": "union", "description": "u",
        "inputs": [{"id": f"in{i}"} for i, _ in enumerate(input_schemas)],
        "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": []},
        "union": {},
    }


def _issues(*, input_schemas):
    return "; ".join(validate_workflow_draft([
        *(source_stage(f"in{i}", schema["columns"])
          for i, schema in enumerate(input_schemas)),
        _union_stage(input_schemas=input_schemas),
    ]))


def test_matching_schemas_ok():
    assert _issues(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]) == ""


def test_three_matching_schemas_ok():
    assert _issues(input_schemas=[_AB_SCHEMA, _AB_SCHEMA, _AB_SCHEMA]) == ""


def test_mismatched_column_set_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}]}  # missing 'b'
    assert "'b'" in _issues(input_schemas=[_AB_SCHEMA, other])


def test_mismatched_column_type_rejected_naming_the_column():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "str", "nullable": True}]}
    assert "'b'" in _issues(input_schemas=[_AB_SCHEMA, other])


def test_mismatch_names_the_disagreeing_input():
    other = {"columns": [{"name": "a", "type": "str", "nullable": True}]}
    assert "in1" in _issues(input_schemas=[_AB_SCHEMA, other])


def test_the_output_is_the_shared_input_schema():
    """What `produces` used to assert is now structural: there is nothing to disagree with."""
    stages = [source_stage("in0", _AB_SCHEMA["columns"]),
              source_stage("in1", _AB_SCHEMA["columns"]),
              _union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA])]
    workflow = parse_workflow(stages)
    union = next(stage for stage in workflow.stages if stage.type == "union")
    resolved = promised_output_schema(union, [
        WorkflowStageInput(id="in0", table_schema=TableSchema(columns=_AB_SCHEMA["columns"])),
        WorkflowStageInput(id="in1", table_schema=TableSchema(columns=_AB_SCHEMA["columns"])),
    ])
    assert [c.name for c in resolved.columns] == ["a", "b"]


def test_needs_at_least_two_inputs():
    with pytest.raises(ValidationError):
        parse_stage(_union_stage(input_schemas=[_AB_SCHEMA]))


def test_round_trips():
    stage = parse_stage(_union_stage(input_schemas=[_AB_SCHEMA, _AB_SCHEMA]))
    dumped = stage.model_dump(by_alias=True)
    reloaded = parse_stage(dumped)
    assert reloaded.type == "union"
    assert reloaded.union is not None
