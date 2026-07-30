"""run_tests_for_stage: execution through the real handlers + normalized comparison."""
from app.models import parse_stage, Stage
from app.runtime.stage_tests import find_failing_stage_tests, run_tests_for_stage

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": True},
]}


def _row_stage(code: str, tests: list[dict]) -> Stage:
    return parse_stage({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {"kind": "inline", "code": code},
        "tests": tests,
    })


_DOUBLE = "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"


def test_matching_test_passes():
    stage = _row_stage(_DOUBLE, [{
        "name": "doubles_two", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 4.0}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed" and not result.diffs


def test_wrong_expected_value_is_mismatch_with_cell_diff():
    stage = _row_stage(_DOUBLE, [{
        "name": "expects_wrong_value", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 5.0}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    [diff] = result.diffs
    assert diff.column == "doubled" and diff.expected == 5.0 and diff.actual == 4.0


def test_raising_function_is_error_with_exception_text():
    stage = _row_stage(
        "def transform(row):\n    raise KeyError('missing_column')\n",
        [{"name": "raises", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": 2.0}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert "KeyError" in (result.message or "")


def test_test_violating_input_schema_is_malformed_not_code_bug():
    # amount is non-nullable; the test's input row is null there.
    stage = _row_stage(_DOUBLE, [{
        "name": "null_amount", "inputs": {"load": [{"amount": None}]},
        "expected": [{"amount": None, "doubled": None}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "malformed"
    assert "null" in (result.message or "").lower()


def test_nan_output_matches_expected_none():
    # Null and NaN are one absence: a float column stores an expected None as
    # NaN, so a NaN result must satisfy a test that expected None.
    stage = _row_stage(
        "def transform(row):\n    return {**row, 'doubled': float('nan')}\n",
        [{"name": "expects_none_gets_nan", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": None}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_nan_output_matches_expected_nan():
    # Plain == would make NaN unequal to itself; the comparison must not.
    stage = _row_stage(
        "def transform(row):\n    return {**row, 'doubled': float('nan')}\n",
        [{"name": "expects_nan_gets_nan", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": float("nan")}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_present_value_does_not_match_absent_expected():
    # The conflation is only null≡NaN; a present value must still fail a test
    # that expected an absence.
    stage = _row_stage(
        "def transform(row):\n    return {**row, 'doubled': 4.0}\n",
        [{"name": "expects_none_gets_value", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": None}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    [diff] = result.diffs
    assert diff.column == "doubled" and diff.actual == 4.0


def test_none_output_matches_expected_none():
    stage = _row_stage(
        "def transform(row):\n    return {**row, 'doubled': None}\n",
        [{"name": "expects_none_gets_none", "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0, "doubled": None}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


_REFUSES = (
    "def transform(row):\n"
    "    raise ValueError('not a dollar amount: 45000 EUR')\n"
)


def test_failure_case_passes_when_message_contains_substring():
    stage = _row_stage(_REFUSES, [{
        "name": "refuses_foreign_currency", "inputs": {"load": [{"amount": 1.0}]},
        "fails_saying": "not a dollar amount",
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_failure_case_mismatches_and_carries_the_actual_message():
    stage = _row_stage(
        "def transform(row):\n    raise KeyError('income')\n",
        [{"name": "refuses_foreign_currency", "inputs": {"load": [{"amount": 1.0}]},
          "fails_saying": "not a dollar amount"}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    assert "income" in (result.message or "")


def test_failure_case_that_returns_rows_is_mismatch():
    stage = _row_stage(_DOUBLE, [{
        "name": "expects_refusal", "inputs": {"load": [{"amount": 2.0}]},
        "fails_saying": "not a dollar amount",
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"
    assert "1 row(s)" in (result.message or "")


def test_rows_case_raising_is_still_error():
    stage = _row_stage(_REFUSES, [{
        "name": "expects_rows", "inputs": {"load": [{"amount": 1.0}]},
        "expected": [{"amount": 1.0, "doubled": 2.0}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert "ValueError" in (result.message or "")


def test_failure_case_substring_match_ignores_case():
    stage = _row_stage(
        "def transform(row):\n    raise ValueError('Not a dollar amount: 45000 EUR')\n",
        [{"name": "refuses_foreign_currency", "inputs": {"load": [{"amount": 1.0}]},
          "fails_saying": "not a dollar amount"}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_failure_case_does_not_match_the_exception_type_name():
    # str(KeyError('income')) is "'income'" — matching the type name too would let
    # a test pin the exception class while pinning nothing about the refusal.
    stage = _row_stage(
        "def transform(row):\n    raise KeyError('income')\n",
        [{"name": "names_the_type", "inputs": {"load": [{"amount": 1.0}]},
          "fails_saying": "KeyError"}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "mismatch"


def test_find_failing_stage_tests_reports_a_failed_failure_case():
    stage = _row_stage(
        "def transform(row):\n    raise KeyError('income')\n",
        [{"name": "refuses_foreign_currency", "inputs": {"load": [{"amount": 1.0}]},
          "fails_saying": "not a dollar amount"}],
    )
    [failure] = find_failing_stage_tests([stage])
    assert "refuses_foreign_currency" in failure and "mismatch" in failure


def _frame_stage(code: str, tests: list[dict]) -> Stage:
    return parse_stage({
        "id": "reshape", "name": "Reshape", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _IN_SCHEMA,
        "function": {"kind": "inline", "code": code},
        "tests": tests,
    })


def test_frame_function_output_order_does_not_matter():
    # The function sorts descending; the test expects ascending order. The
    # type is not order-preserving, so a test must not pin an ordering.
    stage = _frame_stage(
        "def transform(df):\n"
        "    return df.sort_values('amount', ascending=False).reset_index(drop=True)\n",
        [{"name": "order_insensitive",
          "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]},
          "expected": [{"amount": 1.0}, {"amount": 2.0}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_omitted_column_in_expected_row_claims_none():
    # The malformed gate only requires each declared column to appear somewhere
    # in the expected rows; a row that omits a column is claiming None there.
    labelled_schema = {"columns": [
        {"name": "amount", "type": "float", "nullable": False},
        {"name": "label", "type": "str", "nullable": True},
    ]}
    stage = parse_stage({
        "id": "labelled", "name": "Labelled", "type": "python_frame_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": labelled_schema,
        "function": {"kind": "inline", "code": (
            # dtype=object keeps the returned None a real None; pandas' default
            # str dtype would store it as NaN, which is a different value here.
            "import pandas as pd\n"
            "def transform(df):\n"
            "    return pd.DataFrame({\n"
            "        'amount': [1.0, 2.0],\n"
            "        'label': pd.Series(['x', None], dtype=object),\n"
            "    })\n"
        )},
        "tests": [{
            "name": "second_row_omits_label",
            "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]},
            "expected": [{"amount": 1.0, "label": "x"}, {"amount": 2.0}],
        }],
    })
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_frame_function_empty_input_test_runs():
    stage = _frame_stage(
        "def transform(df):\n    return df\n",
        [{"name": "empty_in_empty_out", "inputs": {"load": []}, "expected": []}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed"


def test_row_count_mismatch_reported():
    stage = _frame_stage(
        "def transform(df):\n    return df.head(1)\n",
        [{"name": "expects_all_rows",
          "inputs": {"load": [{"amount": 1.0}, {"amount": 2.0}]},
          "expected": [{"amount": 1.0}, {"amount": 2.0}]}],
    )
    [result] = run_tests_for_stage(stage)
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
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert "NoneType" in (result.message or "")


def test_frame_function_returning_non_dataframe_is_error_not_crash():
    stage = _frame_stage(
        "def transform(df):\n    return {'not': 'a frame'}\n",
        [{"name": "returns_dict",
          "inputs": {"load": [{"amount": 1.0}]},
          "expected": [{"amount": 1.0}]}],
    )
    [result] = run_tests_for_stage(stage)
    assert result.status == "error"
    assert "dict" in (result.message or "")


_LEFT_SCHEMA = {"columns": [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
]}
_RIGHT_SCHEMA = {"columns": [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "label", "type": "str", "nullable": False},
]}
_MERGED_SCHEMA = {"columns": [
    {"name": "id", "type": "str", "nullable": False},
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "label", "type": "str", "nullable": False},
]}


def _multi_input_frame_stage(code: str, tests: list[dict]) -> Stage:
    return parse_stage({
        "id": "merge", "name": "Merge", "type": "python_frame_function",
        "inputs": [
            {"id": "left", "schema": _LEFT_SCHEMA},
            {"id": "right", "schema": _RIGHT_SCHEMA},
        ],
        "output_schema": _MERGED_SCHEMA,
        "function": {"kind": "inline", "code": code},
        "tests": tests,
    })


_MERGE = 'def transform(left_df, right_df):\n    return left_df.merge(right_df, on="id")\n'


def test_multi_input_frame_test_passes():
    stage = _multi_input_frame_stage(_MERGE, [{
        "name": "merges_on_id",
        "inputs": {
            "left": [{"id": "a", "amount": 1.0}],
            "right": [{"id": "a", "label": "widget"}],
        },
        "expected": [{"id": "a", "amount": 1.0, "label": "widget"}],
    }])
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed" and not result.diffs


def test_multi_input_frame_positional_order_is_declared_order():
    # Both inputs share a schema and each carries one distinguishable row.
    # `transform` returns its FIRST positional argument; if the handler
    # passed frames in dict order (or the wrong order) this would return
    # the `right` input's row instead of `left`'s.
    id_schema = {"columns": [{"name": "id", "type": "str", "nullable": False}]}
    stage = parse_stage({
        "id": "first", "name": "First", "type": "python_frame_function",
        "inputs": [
            {"id": "left", "schema": id_schema},
            {"id": "right", "schema": id_schema},
        ],
        "output_schema": id_schema,
        "function": {"kind": "inline", "code": "def transform(a, b):\n    return a\n"},
        "tests": [{
            "name": "returns_first_declared_input",
            "inputs": {
                "left": [{"id": "from_left"}],
                "right": [{"id": "from_right"}],
            },
            "expected": [{"id": "from_left"}],
        }],
    })
    [result] = run_tests_for_stage(stage)
    assert result.status == "passed" and not result.diffs


def test_find_failing_stage_tests_names_stage_and_test():
    green = _row_stage(_DOUBLE, [{
        "name": "doubles_two", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 4.0}],
    }])
    red = _row_stage(_DOUBLE, [{
        "name": "expects_wrong_value", "inputs": {"load": [{"amount": 2.0}]},
        "expected": [{"amount": 2.0, "doubled": 5.0}],
    }])
    failures = find_failing_stage_tests([green, red])
    assert len(failures) == 1
    assert "expects_wrong_value" in failures[0] and "double" in failures[0]


def test_stage_without_tests_contributes_no_failures():
    plain = parse_stage({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _IN_SCHEMA,
    })
    assert find_failing_stage_tests([plain]) == []
