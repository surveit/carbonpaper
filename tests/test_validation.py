"""Tests for app/runtime/validation.validate_dataframe — the dataframe checks
run between stages (schema conformance, nullability, numeric range, enum)."""
from __future__ import annotations

import pandas as pd

from app.models import TableSchema
from app.runtime.validation import validate_dataframe


def _schema(**kw):
    return TableSchema.model_validate(kw)


def test_enum_value_outside_vocabulary_warns():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"]},
    ])
    df = pd.DataFrame({"status": ["open", "pending"]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    msgs = [i.message for i in report.issues if i.column == "status"]
    assert any("enum" in msg for msg in msgs), msgs


def test_enum_all_values_valid_no_warning():
    schema = _schema(columns=[
        {"name": "status", "type": "str", "enum": ["open", "closed"]},
    ])
    df = pd.DataFrame({"status": ["open", "closed"]})
    report = validate_dataframe(df, schema, stage_id="s", phase="output")
    assert [i for i in report.issues if i.column == "status"] == []


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
