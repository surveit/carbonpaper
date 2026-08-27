"""Tests for app/runtime/validation.validate_table — the checks run between
stages (schema conformance, nullability, type, numeric range, enum)."""
from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from app.core.frames import frame_to_table
from app.models import TableSchema
from app.runtime.validation import validate_table


# The fixtures below are pandas because a literal frame reads better than a
# literal table; the validator itself only ever sees the table.
def validate_dataframe(df, schema, *, stage_id, phase):
    return validate_table(frame_to_table(df), schema, stage_id=stage_id, phase=phase)


def _schema(**kw):
    return TableSchema.model_validate(kw)


def _issues_for(column, df, schema):
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    return report, [i for i in report.issues if i.column == column]


def test_enum_value_outside_vocabulary_errors():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"], "nullable": True},
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
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"], "nullable": True},
    ])
    df = pd.DataFrame({"status": ["pending", "pending", "pending", "draft"]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "4 value(s) outside enum ['closed', 'open'] (e.g. 'pending', 'draft')"
    )


def test_enum_error_truncates_a_long_offender_list():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open"], "nullable": True},
    ])
    df = pd.DataFrame({"status": [f"v{n}" for n in range(11)]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "11 value(s) outside enum ['open'] (e.g. 'v0', 'v1', 'v2', 'v3', 'v4', "
        "'v5', 'v6', 'v7', 'v8', 'v9'…)"
    )


def test_exactly_the_sample_size_of_offenders_is_not_truncated():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open"], "nullable": True},
    ])
    df = pd.DataFrame({"status": [f"v{n}" for n in range(10)]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "10 value(s) outside enum ['open'] (e.g. 'v0', 'v1', 'v2', 'v3', 'v4', "
        "'v5', 'v6', 'v7', 'v8', 'v9')"
    )


def test_enum_error_names_the_whole_vocabulary_however_long():
    vocabulary = [f"v{n}" for n in range(9)]
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": vocabulary, "nullable": True},
    ])
    df = pd.DataFrame({"status": ["nope"]})
    _, issues = _issues_for("status", df, schema)
    assert issues[0].message == (
        "1 value(s) outside enum "
        "['v0', 'v1', 'v2', 'v3', 'v4', 'v5', 'v6', 'v7', 'v8'] (e.g. 'nope')"
    )


def test_enum_all_values_valid_raises_no_issue():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"], "nullable": True},
    ])
    df = pd.DataFrame({"status": ["open", "closed"]})
    report, issues = _issues_for("status", df, schema)
    assert issues == []
    assert report.ok


def test_enum_nulls_are_not_reported_as_outside_the_vocabulary():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open"], "nullable": True},
    ])
    df = pd.DataFrame({"status": ["open", None]})
    report, issues = _issues_for("status", df, schema)
    assert issues == []
    assert report.ok


def _range_issues(values, *, bounds, type="int"):
    schema = _schema(columns=[
        {"name": "score", "type": type, "range": bounds, "nullable": True},
    ])
    return _issues_for("score", pd.DataFrame({"score": values}), schema)


def test_a_value_over_the_upper_bound_fails_the_stage():
    report, issues = _range_issues([5, 42], bounds=[0, 10])
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message == "1 value(s) outside range [0, 10]"
    assert not report.ok


def test_a_value_under_the_lower_bound_is_counted_too():
    _, issues = _range_issues([-3, 5], bounds=[0, 10])
    assert issues[0].message == "1 value(s) outside range [0, 10]"


def test_both_bounds_are_counted_together():
    _, issues = _range_issues([-3, 5, 42], bounds=[0, 10])
    assert issues[0].message == "2 value(s) outside range [0, 10]"


def test_the_bounds_themselves_are_inside_the_range():
    report, issues = _range_issues([0, 10], bounds=[0, 10])
    assert issues == [] and report.ok


def test_a_float_column_is_range_checked_like_an_int_one():
    _, issues = _range_issues([0.5, 10.5], bounds=[0, 10], type="float")
    assert issues[0].severity == "error"
    assert issues[0].message == "1 value(s) outside range [0, 10]"


def test_an_unbounded_upper_bound_admits_any_large_value():
    report, issues = _range_issues([1e18], bounds=[0, "+inf"], type="float")
    assert issues == [] and report.ok


def test_an_unbounded_lower_bound_admits_any_small_value():
    report, issues = _range_issues([-1e18], bounds=["-inf", 0], type="float")
    assert issues == [] and report.ok


def test_an_unbounded_upper_bound_leaves_the_lower_one_biting():
    _, issues = _range_issues([-5.0, 1e18], bounds=[0, "+inf"], type="float")
    assert issues[0].message == "1 value(s) outside range [0, +inf]"


def test_an_unbounded_lower_bound_leaves_the_upper_one_biting():
    _, issues = _range_issues([-1e18, 42.0], bounds=["-inf", 10], type="float")
    assert issues[0].message == "1 value(s) outside range [-inf, 10]"


def test_a_null_is_not_counted_as_out_of_range():
    report, issues = _range_issues([5, None], bounds=[0, 10], type="float")
    assert issues == [] and report.ok


def test_no_schema_declared_produces_no_issues():
    # report emits files, not a table, so declaring no schema is the expected case.
    report = validate_dataframe(pd.DataFrame({"a": [1]}), None, stage_id="s", phase="input")
    assert report.issues == []
    assert report.ok
    assert report.rows == 1


def test_missing_declared_column_errors():
    schema = _schema(columns=[{"name": "status", "type": "str", "nullable": True}])
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
    assert issue.message == "2 row(s) have no value, but this column is required"
    assert not report.ok


def test_undeclared_extra_columns_warns():
    schema = _schema(columns=[{"name": "id", "type": "str", "nullable": True}])
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
    schema = _schema(columns=[{"name": "v", "type": type_name, "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": values}), schema)
    assert issues == []
    assert report.ok


def test_bool_column_holding_strings_errors():
    schema = _schema(columns=[{"name": "flag", "type": "bool", "nullable": True}])
    report, issues = _issues_for("flag", pd.DataFrame({"flag": ["yes", "no"]}), schema)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].message == "2 value(s) not of declared type 'bool' (e.g. 'yes', 'no')"
    assert not report.ok


def test_type_mismatch_sample_is_truncated():
    schema = _schema(columns=[{"name": "n", "type": "int", "nullable": True}])
    df = pd.DataFrame({"n": [f"v{n}" for n in range(11)]})
    _, issues = _issues_for("n", df, schema)
    assert issues[0].message == (
        "11 value(s) not of declared type 'int' (e.g. 'v0', 'v1', 'v2', 'v3', 'v4', "
        "'v5', 'v6', 'v7', 'v8', 'v9'…)"
    )


def test_nulls_are_not_reported_as_type_mismatches():
    schema = _schema(columns=[{"name": "v", "type": "str", "nullable": True}])
    df = pd.DataFrame({"v": ["a", None, float("nan"), pd.NaT]})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_null_in_non_nullable_column_reported_once_not_twice():
    schema = _schema(columns=[{"name": "v", "type": "str", "nullable": False}])
    df = pd.DataFrame({"v": ["a", None]})
    report, issues = _issues_for("v", df, schema)
    assert [i.message for i in issues] == ["1 row(s) have no value, but this column is required"]
    assert not report.ok


def test_ints_satisfy_a_float_column():
    schema = _schema(columns=[{"name": "v", "type": "float", "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [1, 2]}), schema)
    assert issues == []
    assert report.ok


def test_int_column_promoted_to_float_by_a_null_is_accepted():
    # pandas widens int64 -> float64 to hold the null; the values are still ints.
    schema = _schema(columns=[{"name": "v", "type": "int", "nullable": True}])
    df = pd.DataFrame({"v": [1, 2, None]})
    assert df["v"].dtype == "float64"
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_fractional_float_in_int_column_errors():
    schema = _schema(columns=[{"name": "v", "type": "int", "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [1, 2.5]}), schema)
    assert issues[0].severity == "error"
    assert "declared type 'int'" in issues[0].message
    assert not report.ok


def test_bool_does_not_satisfy_an_int_column():
    # A Python bool subclasses int; we reject it deliberately.
    schema = _schema(columns=[{"name": "v", "type": "int", "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": [True, False]}), schema)
    assert issues[0].severity == "error"
    assert "declared type 'int'" in issues[0].message
    assert not report.ok


# Each of these is what the column's own arrow type settles, with no cell read:
# the numpy scalars a pandas fixture is built from are gone by the time the
# validator sees the column, because the wire is arrow.
@pytest.mark.parametrize("type_name,values", [
    ("bool", [np.bool_(True), np.bool_(False)]),
    ("int", [np.int64(1), np.int32(2)]),
    ("float", [np.float64(1.5), np.float32(2.5)]),
    ("datetime", [np.datetime64("2024-01-01"), np.datetime64("2024-01-02")]),
])
def test_a_column_arriving_with_its_natural_arrow_type_validates_clean(type_name, values):
    schema = _schema(columns=[{"name": "v", "type": type_name, "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": values}), schema)
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
    schema = _schema(columns=[{"name": "v", "type": type_name, "nullable": True}])
    df = pd.DataFrame({"v": pd.array(raw, dtype=dtype)})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_json_column_accepts_anything():
    schema = _schema(columns=[{"name": "v", "type": "json", "value_type": "str", "nullable": True}])
    # Arrow types this `struct<a: string>`; `json` declares no shape, so whatever
    # arrives is admissible and nothing is reported.
    report, issues = _issues_for("v", pd.DataFrame({"v": [{"a": "b"}, {"a": "c"}]}), schema)
    assert issues == []
    assert report.ok


def test_a_list_json_column_admits_any_element_object():
    schema = _schema(columns=[
        {"name": "v", "type": "list[json]", "value_type": "str", "nullable": True},
    ])
    report, issues = _issues_for("v", pd.DataFrame({"v": [[{"a": "b"}], [{"a": "c"}]]}), schema)
    assert issues == []
    assert report.ok


def test_the_report_dict_names_every_key_the_manifest_reads():
    schema = _schema(columns=[{"name": "n", "type": "int", "nullable": False}])
    report = validate_dataframe(pd.DataFrame({"n": [1, None]}), schema, stage_id="s", phase="output")
    as_dict = report.to_dict()
    assert set(as_dict) == {"stage_id", "phase", "rows", "ok", "issues"}
    assert as_dict["stage_id"] == "s"
    assert as_dict["phase"] == "output"
    assert as_dict["rows"] == 2
    assert as_dict["ok"] is False
    assert set(as_dict["issues"][0]) == {"severity", "column", "message"}
    assert as_dict["issues"][0]["column"] == "n"


def test_list_column_rejects_non_list_values():
    schema = _schema(columns=[{"name": "v", "type": "list[str]", "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": ["nope", "also nope"]}), schema)
    assert issues[0].severity == "error"
    assert issues[0].message == (
        "2 value(s) not of declared type 'list[str]' (e.g. 'nope', 'also nope')"
    )
    assert not report.ok


def test_list_column_checks_element_type():
    schema = _schema(columns=[{"name": "v", "type": "list[int]", "nullable": True}])
    _, issues = _issues_for("v", pd.DataFrame({"v": [["a"], ["b"]]}), schema)
    assert "declared type 'list[int]'" in issues[0].message


def test_datetime_column_rejects_date_strings():
    schema = _schema(columns=[{"name": "v", "type": "datetime", "nullable": True}])
    df = pd.DataFrame({"v": ["2024-01-01", "2024-01-02"]})
    report, issues = _issues_for("v", df, schema)
    assert issues[0].severity == "error"
    assert not report.ok


def test_date_column_accepts_timestamps():
    # pandas has no date-only dtype: a date column round-trips as Timestamp.
    schema = _schema(columns=[{"name": "v", "type": "date", "nullable": True}])
    df = pd.DataFrame({"v": pd.to_datetime(["2024-01-01"])})
    report, issues = _issues_for("v", df, schema)
    assert issues == []
    assert report.ok


def test_empty_dataframe_raises_no_type_issue():
    schema = _schema(columns=[{"name": "v", "type": "int", "nullable": True}])
    report, issues = _issues_for("v", pd.DataFrame({"v": []}), schema)
    assert issues == []
    assert report.ok


# ── the arrow-schema type prover ─────────────────────────────────────────────
# Replaces the pandas-dtype one. pandas parks a list, a dict and mixed junk alike
# in `object`, so the old prover answered "no" for every column that mattered and
# validation fell through to a per-cell Python loop. Arrow types a list column,
# so these are proved from the schema without a cell being read.


def _one_column(name, values, **column):
    return _issues_for(name, pd.DataFrame({name: values}),
                       _schema(columns=[{"name": name, "nullable": True, **column}]))


def test_a_list_of_str_column_is_accepted():
    _, issues = _one_column("tags", [["a", "b"], ["c"]], type="list[str]")
    assert issues == []


def test_a_list_column_holding_the_wrong_element_type_is_still_caught():
    """The prover must not rubber-stamp any list — arrow types the ELEMENT too."""
    _, issues = _one_column("tags", [[1, 2], [3]], type="list[str]")
    assert len(issues) == 1 and "list[str]" in issues[0].message


# pandas promotes an int column to float64 on meeting a null, so a whole-valued
# float there survived a lossy round trip rather than being a type error.
def test_a_declared_int_column_pandas_upcast_to_float_still_passes():
    _, issues = _one_column("n", [1, None, 3], type="int")
    assert issues == []


def test_a_genuinely_fractional_value_in_an_int_column_is_still_caught():
    _, issues = _one_column("n", [1, 1.5], type="int")
    assert len(issues) == 1 and "'int'" in issues[0].message


# A column mixing an int with a str is the reason validation exists: it must be
# reported, never raised.
def test_a_column_whose_type_is_uniform_but_wrong_is_reported_not_crashed():
    _, issues = _one_column("m", ["a", "b"], type="int")
    assert len(issues) == 1 and "not of declared type" in issues[0].message


# A column mixing types has no arrow type, so it cannot reach validation at all —
# `frame_to_table` refuses it at the wire, which is where a stage's output is
# converted. Loud either way; this pins WHICH way, so the next reader does not
# add a validation branch for a case that never arrives.
def test_a_column_of_mixed_types_is_refused_at_the_wire_not_validated():
    import pyarrow as pa

    with pytest.raises(pa.ArrowInvalid):
        frame_to_table(pd.DataFrame({"m": [1, "a"]}))


# ── a json column's declared shape, checked against the arrow struct ──────────
# `Column._json_shape` makes the author declare `fields` or `value_type`; until
# now nothing compared the data to it, so the declaration described the column
# without constraining it.

def _json_column(arrow_values, arrow_type, **column):
    table = pa.table({"v": pa.array(arrow_values, arrow_type)})
    schema = _schema(columns=[{"name": "v", "nullable": True, **column}])
    report = validate_table(table, schema, stage_id="s", phase="output")
    return report, [i for i in report.issues if i.column == "v"]


_ROW_TYPE = pa.struct([("scope", pa.string()), ("value", pa.float64())])
_ROWS = [{"scope": "s1", "value": 1.5}]


def test_a_json_column_matching_its_declared_fields_is_clean():
    report, issues = _json_column(
        _ROWS, _ROW_TYPE, type="json",
        fields=[{"name": "scope", "type": "str", "nullable": True},
                {"name": "value", "type": "float", "nullable": True}],
    )
    assert issues == [] and report.ok


def test_a_declared_json_field_absent_from_the_data_is_an_error():
    _, issues = _json_column(
        _ROWS, _ROW_TYPE, type="json",
        fields=[{"name": "scope", "type": "str", "nullable": True},
                {"name": "currency", "type": "str", "nullable": True}],
    )
    assert [i.severity for i in issues] == ["error", "warning"]
    assert "missing field 'currency'" in issues[0].message


def test_a_json_field_of_the_wrong_type_is_an_error():
    _, issues = _json_column(
        _ROWS, _ROW_TYPE, type="json",
        fields=[{"name": "scope", "type": "str", "nullable": True},
                {"name": "value", "type": "str", "nullable": True}],
    )
    assert issues[0].severity == "error"
    assert "field 'value' is double, not declared type 'str'" in issues[0].message


def test_an_undeclared_json_field_is_a_warning_like_an_undeclared_column():
    _, issues = _json_column(
        _ROWS, _ROW_TYPE, type="json",
        fields=[{"name": "scope", "type": "str", "nullable": True}],
    )
    assert [i.severity for i in issues] == ["warning"]
    assert "undeclared field(s) present: ['value']" in issues[0].message


def test_list_json_checks_the_element_struct():
    _, issues = _json_column(
        [_ROWS], pa.list_(_ROW_TYPE), type="list[json]",
        fields=[{"name": "scope", "type": "str", "nullable": True},
                {"name": "missing", "type": "str", "nullable": True}],
    )
    assert any("missing field 'missing'" in i.message for i in issues)


def test_a_json_column_holding_scalars_is_an_error():
    _, issues = _json_column(["not an object"], pa.string(), type="json",
                             fields=[{"name": "scope", "type": "str", "nullable": True}])
    assert issues[0].severity == "error"
    assert "does not hold objects" in issues[0].message


def test_an_open_map_holds_every_key_to_one_declared_value_type():
    _, issues = _json_column(
        [{"a": "x", "b": "y"}],
        pa.struct([("a", pa.string()), ("b", pa.string())]),
        type="json", value_type="str",
    )
    assert issues == []


def test_an_open_map_field_outside_its_value_type_is_an_error():
    _, issues = _json_column(
        [{"a": "x", "n": 1}],
        pa.struct([("a", pa.string()), ("n", pa.int64())]),
        type="json", value_type="str",
    )
    assert issues[0].severity == "error"
    assert "not of declared value_type 'str'" in issues[0].message and "'n'" in issues[0].message
