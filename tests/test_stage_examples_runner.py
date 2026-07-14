"""run_stage_examples: execution through the real handlers + canonical comparison."""
from app.core.models import Stage
from app.runtime.examples import find_failing_examples, run_stage_examples

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": True},
]}


def _row_stage(code: str, examples: list[dict]) -> Stage:
    return Stage.model_validate({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": code},
        "examples": examples,
    })


_DOUBLE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"


def test_matching_example_passes():
    stage = _row_stage(_DOUBLE, [{
        "name": "doubles_two", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 4.0}],
    }])
    [result] = run_stage_examples(stage)
    assert result.status == "passed" and not result.diffs


def test_wrong_expected_value_is_mismatch_with_cell_diff():
    stage = _row_stage(_DOUBLE, [{
        "name": "expects_wrong_value", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 5.0}],
    }])
    [result] = run_stage_examples(stage)
    assert result.status == "mismatch"
    [diff] = result.diffs
    assert diff.column == "doubled" and diff.expected == 5.0 and diff.actual == 4.0


def test_raising_function_is_error_with_exception_text():
    stage = _row_stage(
        "def transform(row):\n    raise KeyError('missing_column')\n",
        [{"name": "raises", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": 2.0}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "error"
    assert "KeyError" in (result.message or "")


def test_example_violating_input_schema_is_malformed_not_code_bug():
    # amount is non-nullable; the example's input row is null there.
    stage = _row_stage(_DOUBLE, [{
        "name": "null_amount", "inputs": {"load": [{"amount": None}]},
        "expected": [{"amount": None, "doubled": None}],
    }])
    [result] = run_stage_examples(stage)
    assert result.status == "malformed"
    assert "null" in (result.message or "").lower()


def test_nan_output_matches_expected_null():
    stage = _row_stage(
        "def transform(row):\n    return {**row, 'doubled': float('nan')}\n",
        [{"name": "nan_normalizes_to_null", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": None}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "passed"


def _frame_stage(code: str, examples: list[dict]) -> Stage:
    return Stage.model_validate({
        "id": "reshape", "name": "Reshape", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _IN_SCHEMA,
        "function": {"kind": "inline", "code": code},
        "examples": examples,
    })


def test_frame_function_output_order_does_not_matter():
    # The function sorts descending; the example expects ascending order. The
    # type is not order-preserving, so an example must not pin an ordering.
    stage = _frame_stage(
        "def transform(df):\n"
        "    return df.sort_values('amount', ascending=False).reset_index(drop=True)\n",
        [{"name": "order_insensitive",
          "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]},
          "expected": [{"amount": 1.0}, {"amount": 2.0}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "passed"


def test_frame_function_empty_input_example_runs():
    stage = _frame_stage(
        "def transform(df):\n    return df\n",
        [{"name": "empty_in_empty_out", "inputs": {"load": []}, "expected": []}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "passed"


def test_row_count_mismatch_reported():
    stage = _frame_stage(
        "def transform(df):\n    return df.head(1)\n",
        [{"name": "expects_all_rows",
          "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]},
          "expected": [{"amount": 1.0}, {"amount": 2.0}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "mismatch"
    assert "2 row(s)" in (result.message or "") and "1" in (result.message or "")


def test_frame_function_returning_none_is_error_not_crash():
    # A very common authoring mistake: mutating in place (inplace=True) rather
    # than returning the transformed frame. This must surface as an `error`
    # result, not raise out of the runner.
    stage = _frame_stage(
        "def transform(df):\n    df.sort_values('amount', inplace=True)\n",
        [{"name": "mutates_in_place",
          "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "error"
    assert "NoneType" in (result.message or "")


def test_frame_function_returning_non_dataframe_is_error_not_crash():
    stage = _frame_stage(
        "def transform(df):\n    return {'not': 'a frame'}\n",
        [{"name": "returns_dict",
          "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0}]}],
    )
    [result] = run_stage_examples(stage)
    assert result.status == "error"
    assert "dict" in (result.message or "")


def test_find_failing_examples_names_stage_and_example():
    green = _row_stage(_DOUBLE, [{
        "name": "doubles_two", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 4.0}],
    }])
    red = _row_stage(_DOUBLE, [{
        "name": "expects_wrong_value", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 5.0}],
    }])
    failures = find_failing_examples([green, red])
    assert len(failures) == 1
    assert "expects_wrong_value" in failures[0] and "double" in failures[0]


def test_stage_without_examples_contributes_no_failures():
    plain = Stage.model_validate({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "computed_static"},
    })
    assert find_failing_examples([plain]) == []
