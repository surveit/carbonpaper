"""StageTest shape checks + the Stage.tests field's serialization contract."""
import pytest
from pydantic import ValidationError

from app.core.models import Stage, StageTest
from app.core.models.stages.stage_tests import build_stage_tests_model
from app.services.loader import stage_to_spec_dict

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}

def _row_stage(tests=None) -> dict:
    stage = {
        "id": "double", "name": "Double the amount", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
    }
    if tests is not None:
        stage["tests"] = tests
    return stage

_GOOD_TEST = {
    "name": "doubles_a_positive_amount",
    "description": "The basic contract: doubled = amount * 2.",
    "inputs": {"load": [{"amount": 2.0}]},
    "expected": [{"amount": 2.0, "doubled": 4.0}],
}


def test_valid_test_parses_on_python_row_stage():
    stage = Stage.model_validate(_row_stage([_GOOD_TEST]))
    assert stage.tests is not None
    test: StageTest = stage.tests[0]
    assert test.name == "doubles_a_positive_amount"


def test_tests_rejected_on_non_python_stage():
    bad = {
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "tests": [{"name": "x", "inputs": {}, "expected": []}],
    }
    with pytest.raises(ValidationError, match="python transforms"):
        Stage.model_validate(bad)


def test_test_inputs_must_match_declared_inputs():
    wrong_key = {**_GOOD_TEST, "inputs": {"not_load": [{"amount": 1.0}]}}
    with pytest.raises(ValidationError, match="declared inputs"):
        Stage.model_validate(_row_stage([wrong_key]))


def test_multi_input_test_missing_one_input_is_rejected():
    left_schema = {"columns": [{"name": "id", "type": "str", "nullable": False}]}
    right_schema = {"columns": [{"name": "id", "type": "str", "nullable": False}]}
    stage = {
        "id": "merge", "name": "Merge", "type": "python_frame_function",
        "inputs": [
            {"id": "left", "schema": left_schema},
            {"id": "right", "schema": right_schema},
        ],
        "output_schema": left_schema,
        "function": {"kind": "inline", "code": "def transform(a, b):\n    return a\n"},
        "tests": [{
            "name": "only_left_supplied",
            "inputs": {"left": [{"id": "x"}]},
            "expected": [{"id": "x"}],
        }],
    }
    with pytest.raises(ValidationError, match="declared inputs"):
        Stage.model_validate(stage)


def test_row_function_test_is_one_row_in_one_row_out():
    two_rows = {**_GOOD_TEST,
                "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]}}
    with pytest.raises(ValidationError, match="one row"):
        Stage.model_validate(_row_stage([two_rows]))


def test_duplicate_test_names_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        Stage.model_validate(_row_stage([_GOOD_TEST, dict(_GOOD_TEST)]))


def test_stage_without_tests_serializes_without_tests_key():
    # stage_to_spec_dict feeds the node belief hash: adding the field must not
    # change the dump of any existing stage, or all approvals drop to stale.
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage()))
    assert "tests" not in spec


def test_empty_tests_list_normalizes_to_absent():
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage([])))
    assert "tests" not in spec


def test_tests_round_trip_through_spec_dict():
    spec = stage_to_spec_dict(Stage.model_validate(_row_stage([_GOOD_TEST])))
    reloaded = Stage.model_validate(spec)
    assert reloaded.tests is not None
    assert reloaded.tests[0].inputs == {"load": [{"amount": 2.0}]}
    assert reloaded.tests[0].expected == [{"amount": 2.0, "doubled": 4.0}]


def test_stage_tests_model_accepts_a_valid_suite():
    model = build_stage_tests_model("python_row_function", ["load"])
    suite = model.model_validate({"tests": [_GOOD_TEST]})
    assert suite.tests[0].name == _GOOD_TEST["name"]


def test_stage_tests_model_rejects_wrong_input_ids():
    model = build_stage_tests_model("python_row_function", ["load"])
    bad = dict(_GOOD_TEST, inputs={"ghost": [{"amount": 2.0}]})
    with pytest.raises(ValidationError, match="declared inputs"):
        model.model_validate({"tests": [bad]})


def test_stage_tests_model_rejects_row_function_fan_out():
    model = build_stage_tests_model("python_row_function", ["load"])
    bad = dict(_GOOD_TEST, expected=[{"amount": 2.0, "doubled": 4.0},
                                     {"amount": 3.0, "doubled": 6.0}])
    with pytest.raises(ValidationError, match="one row in"):
        model.model_validate({"tests": [bad]})
