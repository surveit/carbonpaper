"""Tests for app/services/table_check.py — read a tabular file and check it
against a declared TableSchema."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.models import FileFormat, TableSchema
from app.services.table_check import read_table, table_columns, validate_table_file


def _schema(**kw: object) -> TableSchema:
    cols = kw.pop("columns")
    return TableSchema.model_validate({"columns": cols, **kw})


def test_valid_csv_matches_schema(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("k,v\n1,2\n3,4\n")
    schema = _schema(columns=[{"name": "k", "type": "int"},
                              {"name": "v", "type": "int"}])
    report = validate_table_file(path, FileFormat.csv, schema)
    assert report.ok is True
    assert report.rows == 2


def test_missing_declared_column(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("k\n1\n")
    schema = _schema(columns=[{"name": "k", "type": "int"},
                              {"name": "v", "type": "int"}])
    report = validate_table_file(path, FileFormat.csv, schema)
    assert report.ok is False
    assert any(i.column == "v" for i in report.issues)


def test_null_in_non_nullable_column(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("k,v\n1,\n3,4\n")
    schema = _schema(columns=[{"name": "k", "type": "int"},
                              {"name": "v", "type": "int", "nullable": False}])
    report = validate_table_file(path, FileFormat.csv, schema)
    assert report.ok is False
    assert any(i.severity == "error" and i.column == "v" for i in report.issues)


def test_header_only_file_has_zero_rows(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("k,v\n")
    schema = _schema(columns=[{"name": "k", "type": "int"},
                              {"name": "v", "type": "int"}])
    report = validate_table_file(path, FileFormat.csv, schema)
    assert report.rows == 0


def test_fully_empty_file_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("")
    schema = _schema(columns=[{"name": "k", "type": "int"}])
    with pytest.raises(ValueError, match=str(path).replace("\\", "\\\\")):
        validate_table_file(path, FileFormat.csv, schema)


def test_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    path = tmp_path / "does_not_exist.csv"
    schema = _schema(columns=[{"name": "k", "type": "int"}])
    with pytest.raises(FileNotFoundError, match=str(path).replace("\\", "\\\\")):
        validate_table_file(path, FileFormat.csv, schema)


def test_table_columns_returns_header_list(tmp_path: Path) -> None:
    path = tmp_path / "cases.csv"
    path.write_text("k,v,quote\n1,2,hello\n")
    assert table_columns(path, FileFormat.csv) == ["k", "v", "quote"]


def test_read_table_missing_file_raises_file_not_found(tmp_path: Path) -> None:
    path = tmp_path / "nope.csv"
    with pytest.raises(FileNotFoundError, match=str(path).replace("\\", "\\\\")):
        read_table(path, FileFormat.csv)


def test_read_table_unsupported_format_raises_value_error(tmp_path: Path) -> None:
    path = tmp_path / "cases.geojson"
    path.write_text("{}")
    with pytest.raises(ValueError, match="geojson"):
        read_table(path, FileFormat.geojson)
