"""StageTest shape checks + the Stage.tests field's serialization contract."""
import pytest
from pydantic import ValidationError

from app.models import parse_stage, StageTest, TableSchema
from app.models.stages.stage_tests import build_stage_tests_model
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
    stage = parse_stage(_row_stage([_GOOD_TEST]))
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
        parse_stage(bad)


def test_test_inputs_must_match_declared_inputs():
    wrong_key = {**_GOOD_TEST, "inputs": {"not_load": [{"amount": 1.0}]}}
    with pytest.raises(ValidationError, match="declared inputs"):
        parse_stage(_row_stage([wrong_key]))


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
        parse_stage(stage)


def test_row_function_test_is_one_row_in_one_row_out():
    two_rows = {**_GOOD_TEST,
                "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]}}
    with pytest.raises(ValidationError, match="one row"):
        parse_stage(_row_stage([two_rows]))


def test_duplicate_test_names_rejected():
    with pytest.raises(ValidationError, match="duplicate"):
        parse_stage(_row_stage([_GOOD_TEST, dict(_GOOD_TEST)]))


def test_stage_without_tests_serializes_without_tests_key():
    # stage_to_spec_dict feeds the node belief hash: adding the field must not
    # change the dump of any existing stage, or all approvals drop to stale.
    spec = stage_to_spec_dict(parse_stage(_row_stage()))
    assert "tests" not in spec


def test_empty_tests_list_normalizes_to_absent():
    spec = stage_to_spec_dict(parse_stage(_row_stage([])))
    assert "tests" not in spec


def test_tests_round_trip_through_spec_dict():
    spec = stage_to_spec_dict(parse_stage(_row_stage([_GOOD_TEST])))
    reloaded = parse_stage(spec)
    assert reloaded.tests is not None
    assert reloaded.tests[0].inputs == {"load": [{"amount": 2.0}]}
    assert reloaded.tests[0].expected == [{"amount": 2.0, "doubled": 4.0}]


def _row_suite_model():
    return build_stage_tests_model(
        "python_row_function",
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(_OUT_SCHEMA),
    )


def test_stage_tests_model_accepts_a_valid_suite():
    suite = _row_suite_model().model_validate({"tests": [_GOOD_TEST]})
    assert suite.tests[0].name == _GOOD_TEST["name"]


def test_stage_tests_model_rejects_wrong_input_ids():
    bad = dict(_GOOD_TEST, inputs={"ghost": [{"amount": 2.0}]})
    with pytest.raises(ValidationError, match="declared inputs"):
        _row_suite_model().model_validate({"tests": [bad]})


def test_stage_tests_model_rejects_row_function_fan_out():
    bad = dict(_GOOD_TEST, expected=[{"amount": 2.0, "doubled": 4.0},
                                     {"amount": 3.0, "doubled": 6.0}])
    with pytest.raises(ValidationError, match="one row in"):
        _row_suite_model().model_validate({"tests": [bad]})


def test_stage_tests_model_rejects_undeclared_column_in_an_input_row():
    bad = dict(_GOOD_TEST, inputs={"load": [{"amount": 2.0, "currency": "USD"}]})
    with pytest.raises(ValidationError) as excinfo:
        _row_suite_model().model_validate({"tests": [bad]})
    message = str(excinfo.value)
    assert "doubles_a_positive_amount" in message
    assert "load" in message
    assert "currency" in message


def test_stage_tests_model_rejects_undeclared_column_in_expected_rows():
    bad = dict(_GOOD_TEST,
               expected=[{"amount": 2.0, "doubled": 4.0, "tripled": 6.0}])
    with pytest.raises(ValidationError) as excinfo:
        _row_suite_model().model_validate({"tests": [bad]})
    message = str(excinfo.value)
    assert "doubles_a_positive_amount" in message
    assert "tripled" in message


def test_stage_tests_model_rejects_missing_declared_column_in_expected_rows():
    bad = dict(_GOOD_TEST, expected=[{"amount": 2.0}])
    with pytest.raises(ValidationError) as excinfo:
        _row_suite_model().model_validate({"tests": [bad]})
    assert "doubled" in str(excinfo.value)


def test_stage_tests_model_rejects_a_wrongly_typed_cell():
    bad = dict(_GOOD_TEST, inputs={"load": [{"amount": "two"}]})
    with pytest.raises(ValidationError) as excinfo:
        _row_suite_model().model_validate({"tests": [bad]})
    message = str(excinfo.value)
    assert "doubles_a_positive_amount" in message
    assert "load" in message
    assert "amount" in message


def _frame_suite_model(in_schema: dict) -> type:
    return build_stage_tests_model(
        "python_frame_function",
        {"load": TableSchema.model_validate(in_schema)},
        TableSchema.model_validate(in_schema),
    )


_TWO_COLUMN_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "label", "type": "str", "nullable": True},
]}


def test_stage_tests_model_rejects_a_row_omitting_a_column_another_row_supplies():
    """Every row states the whole schema. A nullable column is an explicit None,
    not an absent key — the union of a case's rows is not enough."""
    bad = {
        "name": "second_row_drops_label",
        "inputs": {"load": [{"amount": 1.0, "label": "a"}, {"amount": 2.0}]},
        "expected": [{"amount": 1.0, "label": "a"}, {"amount": 2.0, "label": None}],
    }
    with pytest.raises(ValidationError) as excinfo:
        _frame_suite_model(_TWO_COLUMN_SCHEMA).model_validate({"tests": [bad]})
    message = str(excinfo.value)
    assert "second_row_drops_label" in message
    assert "load" in message
    assert "label" in message


def test_stage_tests_model_accepts_an_explicit_null_in_a_nullable_column():
    suite = _frame_suite_model(_TWO_COLUMN_SCHEMA).model_validate({"tests": [{
        "name": "label_may_be_null",
        "inputs": {"load": [{"amount": 1.0, "label": None}]},
        "expected": [{"amount": 1.0, "label": None}],
    }]})
    assert suite.tests[0].inputs["load"] == [{"amount": 1.0, "label": None}]


_FAILURE_TEST = {
    "name": "another_currency_is_not_recorded_as_dollars",
    "inputs": {"load": [{"amount": 2.0}]},
    "expected": None,
}


def test_row_function_failure_case_needs_no_expected_row():
    """One row in and no rows out is the point of a failure case, so the
    one-row-in-one-row-out rule does not apply to it."""
    suite = _row_suite_model().model_validate({"tests": [_FAILURE_TEST]})
    assert suite.tests[0].expected is None


def test_row_function_failure_case_still_needs_exactly_one_input_row():
    two_rows = dict(_FAILURE_TEST, inputs={"load": [{"amount": 1.0}, {"amount": 2.0}]})
    with pytest.raises(ValidationError, match="one row in"):
        _row_suite_model().model_validate({"tests": [two_rows]})


def test_failure_case_input_rows_are_still_schema_checked():
    bad = dict(_FAILURE_TEST, inputs={"load": [{"amount": "two"}]})
    with pytest.raises(ValidationError) as excinfo:
        _row_suite_model().model_validate({"tests": [bad]})
    message = str(excinfo.value)
    assert "another_currency_is_not_recorded_as_dollars" in message
    assert "load" in message
    assert "amount" in message


def test_a_test_omitting_expected_is_rejected():
    """`expected` has no default: a case that forgets it must be rejected outright
    rather than read as the claim that the step fails."""
    missing = {k: v for k, v in _GOOD_TEST.items() if k != "expected"}
    with pytest.raises(ValidationError, match="expected"):
        _row_suite_model().model_validate({"tests": [missing]})


def test_zero_expected_rows_is_not_a_failure_claim():
    """[] and null are different claims: a frame step legitimately returns no rows,
    and that case must survive validation as a rows case."""
    suite = _frame_suite_model(_IN_SCHEMA).model_validate({"tests": [{
        "name": "filters_everything_out",
        "inputs": {"load": [{"amount": 1.0}]},
        "expected": [],
    }]})
    assert suite.tests[0].expected == []


def test_failure_case_survives_the_spec_dict_round_trip():
    """The dump drops None-valued keys, so `expected: null` has to be written out
    explicitly — dropped, it would reload as a case that forgot the field."""
    stage = parse_stage(_row_stage([_FAILURE_TEST]))
    spec = stage_to_spec_dict(stage)
    assert spec["tests"][0]["expected"] is None
    reloaded = parse_stage(spec)
    assert reloaded.tests is not None
    assert reloaded.tests[0].expected is None


def test_rows_case_wire_form_is_unchanged():
    stage = parse_stage(_row_stage([_GOOD_TEST]))
    assert stage.tests is not None
    assert stage.tests[0].expected == [{"amount": 2.0, "doubled": 4.0}]
    assert stage_to_spec_dict(stage)["tests"][0]["expected"] == [
        {"amount": 2.0, "doubled": 4.0}
    ]


def test_stage_tests_model_accepts_an_empty_input_case():
    """No rows means no columns to disagree with — an "empty upstream" case is
    legitimate, and the runtime builds its frame from the declared schema."""
    model = build_stage_tests_model(
        "python_frame_function",
        {"load": TableSchema.model_validate(_IN_SCHEMA)},
        TableSchema.model_validate(_OUT_SCHEMA),
    )
    suite = model.model_validate({"tests": [{
        "name": "empty_input_yields_no_rows",
        "inputs": {"load": []},
        "expected": [],
    }]})
    assert suite.tests[0].inputs == {"load": []}
