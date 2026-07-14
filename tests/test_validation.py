"""Tests for app/runtime/validation.validate_dataframe — the dataframe checks
run between stages (schema conformance, nullability, numeric range, enum)."""
from __future__ import annotations

import pandas as pd

from app.core.models import TableSchema
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
