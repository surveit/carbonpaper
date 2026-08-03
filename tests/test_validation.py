"""Tests for app/runtime/validation.validate_dataframe — the dataframe checks
run between stages (schema conformance, nullability, type, numeric range,
enum)."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pytest

from app.models import TableSchema
from app.runtime.validation import validate_dataframe


def _schema(**kw):
    return TableSchema.model_validate(kw)


def _issues_for(column, df, schema):
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    return report, [i for i in report.issues if i.column == column]


def test_enum_value_outside_vocabulary_errors():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"]},
    ])
    df = pd.DataFrame({"status": ["open", "pending"]})
    report, issues = _issues_for("status", df, schema)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message == (
        "1 value(s) outside enum ['closed', 'open'] (e.g. 'pending')"
    )
    assert not report.ok


def test_enum_error_names_the_distinct_offenders_not_the_repeats():
    """One typo on 3 rows is one thing to fix; the count carries how many rows."""
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"]},
    ])
    df = pd.DataFrame({"status": ["pending", "pending", "pending", "draft"]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "4 value(s) outside enum ['closed', 'open'] (e.g. 'pending', 'draft')"
    )


def test_enum_error_truncates_a_long_offender_list():
    schema = _schema(columns=[{"name": "status", "type": "str", "enum": ["open"]}])
    df = pd.DataFrame({"status": [f"v{n}" for n in range(11)]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "11 value(s) outside enum ['open'] (e.g. 'v0', 'v1', 'v2', 'v3', 'v4', "
        "'v5', 'v6', 'v7', 'v8', 'v9'…)"
    )


def test_enum_error_names_the_whole_vocabulary_however_long():
    """The set is never sampled — what is too long to show is the display's call."""
    vocabulary = [f"v{n}" for n in range(9)]
    schema = _schema(columns=[{"name": "status", "type": "str", "enum": vocabulary}])
    df = pd.DataFrame({"status": ["nope"]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "1 value(s) outside enum "
        "['v0', 'v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8'] (e.g. 'nope')"
    )


def test_enum_all_values_valid_raises_no_issue():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"]},
    ])
    df = pd.DataFrame({"status": ["open", "closed"]})
    report, issues = _issues_for("status", df, schema)
    assert issues == []
    assert report.ok


def test_enum_nulls_are_not_reported_as_outside_the_vocabulary():
    """A nullable enum column may hold nulls; `_find_nullability_issues` owns the rest."""
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open"], "nullable": True},
    ])
    df = pd.DataFrame({"status": ["open", None]})
    report, issues = _issues_for("status", df, schema)
    assert issues == []
    assert report.ok


def test_numeric_range_value_outside_bounds_warns():
    schema = _schema(columns=[{"name": "score", "type": "int", "range": [0, 10]}])
    df = pd.DataFrame({"score": [5, 42]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    msgs = [i.message for i in report.issues if i.column == "score"]
    assert any("range" in msg for msg in msgs), msgs


def test_no_schema_declared_returns_single_warning_and_no_error():
    report = validate_dataframe(pd.DataFrame({"a": [1]}), None, stage_id="s", phase="input")
    assert len(report.issues) == 1
    issue = report.issues[0]
    assert issue.severity == "warning"
    assert issue.column is None
    assert issue.message == "No schema declared; skipping checks."
    assert report.ok


def test_missing_declared_column_errors():
    schema = _schema(columns=[{"name": "status", "type": "str"}])
    report = validate_dataframe(pd.DataFrame({"other": [1]}), schema, stage_id="s", phase="output")
    issue = next(i for i in report.issues if i.column == "status")
    assert issue.severity == "error"
    assert issue.message == "Missing column 'status'"
    assert not report.ok


def test_non_nullable_column_with_nulls_errors():
    schema = _schema(columns=[{"name": "status", "type": "str", "nullable": False}])
    df = pd.DataFrame({"status": ["open", None, None]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    issue = next(i for i in report.issues if i.column == "status")
    assert issue.severity == "error"
    assert issue.message == "2 null value(s) in non-nullable column"
    assert not report.ok


def test_primary_key_duplicated_errors():
    schema = _schema(columns=[{"name": "id", "type": "str"}], primary_key=["id"])
    df = pd.DataFrame({"id": ["a", "a", "b"]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    issue = next(i for i in report.issues if i.column == "id")
    assert issue.severity == "error"
    assert issue.message == "Primary key duplicated on 1 row(s)"
    assert not report.ok


def test_undeclared_extra_columns_warns():
    schema = _schema(columns=[{"name": "id", "type": "str"}])
    df = pd.DataFrame({"id": ["a"], "extra": [1]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    issue = next(i for i in report.issues if i.column is None and "undeclared" in i.message)
    assert issue.severity == "warning"
    assert issue.message == "1 undeclared column(s) present (will be passed through): ['extra']"
    assert report.ok


# ── Declared-type checks (_find_type_issues) ─────────────────────────────────

@pytest.mark.parametrize("type_name,values", [
    ("str", ["a", "b"]),
    ("int", [1, 2]),
    ("float", [1.5, 2.5]),
    ("bool", [True, False]),
    ("datetime", pd.to_datetime(["2024-01-01 10:00", "2024-02-02 11:00"])),
    ("date", [dt.date(2024, 1, 1), dt.date(2024, 1, 2)]),
])
def test_each_declared_type_accepts_its_own_values(type_name, values):
    schema = _schema(columns=[{"name": "v", "type": type_name}])
    report, issues = _issues_for("v", pd.DataFrame({"v": values}), schema)
    assert issues == []
    assert report.ok


def test_bool_column_holding_strings_errors():
    schema = _schema(columns=[{"name": "flag", "type": "bool"}])
    df = pd.DataFrame({"flag": ["yes", "no", True]})
    report, issues = _issues_for("flag", df, schema)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message == "2 value(s) not of declared type 'bool' (e.g. 'yes', 'no')"
    assert not report.ok


def test_type_mismatch_sample_is_truncated():
    schema = _schema(columns=[{"name": "n", "type": "int"}])
    df = pd.DataFrame({"n": [f"v{n}" for n in range(11)]})
    _, issues = _issues_for("n", df, schema)
    assert issues[0].message == (
        "11 value(s) not of declared type 'int' (e.g. 'v0', 'v1', 'v2', 'v3', 'v4', "
        "'v5', 'v6', 'v7', 'v8', 'v9'…)"
    )


def test_nulls_are_not_reported_as_type_mismatches():
    schema = _schema(columns=[{"name": "v", "type": "str"}])
    df = pd.DataFrame({"v": ["a", None, float("nan"), pd.NaT]})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_null_in_non_nullable_column_reported_once_not_twice():
    schema = _schema(columns=[{"name": "v", "type": "str", "nullable": False}])
    df = pd.DataFrame({"v": ["a", None]})
    report, issues = _issues_for("v", df, schema)
    assert [i.message for i in issues] == ["1 null value(s) in non-nullable column"]
    assert not report.ok


def test_ints_satisfy_a_float_column():
    schema = _schema(columns=[{"name": "v", "type": "float"}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [1, 2]}), schema)
    assert issues == []
    assert report.ok


def test_int_column_promoted_to_float_by_a_null_is_accepted():
    # pandas widens int64 -> float64 to hold the null; the values are still ints.
    schema = _schema(columns=[{"name": "v", "type": "int"}])
    df = pd.DataFrame({"v": [1, 2, None]})
    assert df["v"].dtype == "float64"
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_fractional_float_in_int_column_errors():
    schema = _schema(columns=[{"name": "v", "type": "int"}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [1, 2.5]}), schema)
    assert issues[0].severity == "error"
    assert "declared type 'int'" in issues[0].message
    assert not report.ok


def test_bool_does_not_satisfy_an_int_column():
    # A Python bool subclasses int; we reject it deliberately.
    schema = _schema(columns=[{"name": "v", "type": "int"}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [True, False]}), schema)
    assert issues[0].severity == "error"
    assert "declared type 'int'" in issues[0].message
    assert not report.ok


@pytest.mark.parametrize("type_name,values", [
    ("bool", [np.bool_(True), np.bool_(False)]),
    ("int", [np.int64(1), np.int32(2)]),
    ("float", [np.float64(1.5), np.float32(2.5)]),
    ("datetime", [np.datetime64("2024-01-01"), np.datetime64("2024-01-02")]),
])
def test_numpy_scalars_satisfy_their_logical_type(type_name, values):
    schema = _schema(columns=[{"name": "v", "type": type_name}])
    df = pd.DataFrame({"v": pd.Series(values, dtype=object)})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


@pytest.mark.parametrize("type_name,dtype", [
    ("bool", "boolean"),
    ("int", "Int64"),
    ("float", "Float64"),
    ("str", "string"),
])
def test_nullable_extension_dtypes_satisfy_their_logical_type(type_name, dtype):
    raw = {"boolean": [True, None], "Int64": [1, None],
           "Float64": [1.5, None], "string": ["a", None]}[dtype]
    schema = _schema(columns=[{"name": "v", "type": type_name}])
    df = pd.DataFrame({"v": pd.array(raw, dtype=dtype)})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_json_column_accepts_anything():
    schema = _schema(columns=[{"name": "v", "type": "json", "value_type": "str"}])
    df = pd.DataFrame({"v": [{"a": "b"}, "not a dict", 7]})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_list_column_rejects_non_list_values():
    schema = _schema(columns=[{"name": "v", "type": "list[str]"}])
    df = pd.DataFrame({"v": [["a", "b"], "nope"]})
    report, issues = _issues_for("v", df, schema)
    assert issues[0].severity == "error"
    assert issues[0].message == "1 value(s) not of declared type 'list[str]' (e.g. 'nope')"
    assert not report.ok


def test_list_column_checks_element_type():
    schema = _schema(columns=[{"name": "v", "type": "list[int]"}])
    df = pd.DataFrame({"v": [[1, 2], ["a"]]})
    _, issues = _issues_for("v", df, schema)
    assert "declared type 'list[int]'" in issues[0].message


def test_datetime_column_rejects_date_strings():
    schema = _schema(columns=[{"name": "v", "type": "datetime"}])
    df = pd.DataFrame({"v": ["2024-01-01", "2024-01-02"]})
    report, issues = _issues_for("v", df, schema)
    assert issues[0].severity == "error"
    assert not report.ok


def test_date_column_accepts_timestamps():
    # pandas has no date-only dtype: a date column round-trips as Timestamp.
    schema = _schema(columns=[{"name": "v", "type": "date"}])
    df = pd.DataFrame({"v": pd.to_datetime(["2024-01-01"])})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_empty_dataframe_raises_no_type_issue():
    schema = _schema(columns=[{"name": "v", "type": "int"}])
    report, issues = _issues_for("v", pd.DataFrame({"v": []}), schema)
    assert issues == []
    assert report.ok
