"""StageExample shape checks + the Stage.examples field's serialization contract."""
import pytest
from pydantic import ValidationError

from app.core.models import Stage, StageExample
from app.services.loader import stage_to_spec_dict

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}

def _row_stage(examples=None) -> dict:
    stage = {
        "id": "double", "name": "Double the amount", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    }
    if examples is not None:
        stage["examples"] = examples
    return stage

_GOOD_EXAMPLE = {
    "name": "doubles_a_positive_amount",
    "description": "The basic contract: doubled = amount * 2.",
    "inputs": {"load": [{"amount": 2.0}]},
    "expected": [{"amount": 2.0, "doubled": 4.0}],
}


def test_valid_example_parses_on_python_row_stage():
    stage = Stage.model_validate(_row_stage([_GOOD_EXAMPLE]))
    assert stage.examples is not None
    example: StageExample = stage.examples[0]
    assert example.name == "doubles_a_positive_amount"


def test_examples_rejected_on_non_python_stage():
    bad = {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "computed_static"},
        "examples": [{"name": "x", "inputs": {}, "expected": []}],
    }
    with pytest.raises(ValidationError, match="python transforms"):
        Stage.model_validate(bad)


def test_example_inputs_must_match_declared_inputs():
    wrong_key = {**_GOOD_EXAMPLE, "inputs": {"not_load": [{"amount": 1.0}]}}
    with pytest.raises(ValidationError, match="declared inputs"):
        Stage.model_validate(_row_stage([wrong_key]))


def test_row_function_example_is_one_row_in_one_row_out():
    two_rows = {**_GOOD_EXAMPLE,
                "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]}}
    with pytest.raises(ValidationError, match="one row"):
        Stage.model_validate(_row_stage([two_rows]))


def test_duplicate_example_names_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        Stage.model_validate(_row_stage([_GOOD_EXAMPLE, dict(_GOOD_EXAMPLE)]))


def test_stage_without_examples_serializes_without_examples_key():
    # stage_to_spec_dict feeds the node belief hash: adding the field must not
    # change the dump of any existing stage, or all approvals drop to stale.
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage()))
    assert "examples" not in spec


def test_empty_examples_list_normalizes_to_absent():
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage([])))
    assert "examples" not in spec


def test_examples_round_trip_through_spec_dict():
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage([_GOOD_EXAMPLE])))
    reloaded = Stage.model_validate(spec)
    assert reloaded.examples is not None
    assert reloaded.examples[0].inputs == {"load": [{"amount": 2.0}]}
    assert reloaded.examples[0].expected == [{"amount": 2.0, "doubled": 4.0}]
