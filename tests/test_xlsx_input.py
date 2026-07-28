"""_read_xlsx maps connector params onto pd.read_excel (app/runtime/stages/input_data.py)."""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from app.models.stage import FileFormat
from app.runtime.stages.input_data import _read_xlsx


def _write(tmp_path: Path, rows: list[list[object]], sheets: dict[str, list[list[object]]] | None = None) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None
    ws.title = "Sheet1"
    for row in rows:
        ws.append(row)
    for name, extra in (sheets or {}).items():
        other = wb.create_sheet(name)
        for row in extra:
            other.append(row)
    path = tmp_path / "book.xlsx"
    wb.save(path)
    return path


def test_xlsx_is_a_declarable_format():
    assert FileFormat("xlsx") is FileFormat.xlsx


def test_reads_first_sheet_with_header_on_row_one(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000], ["BETA", 2000]])
    df = _read_xlsx(path, {})
    assert list(df.columns) == ["client", "income"]
    assert len(df) == 2
    assert df.iloc[1]["client"] == "BETA"
    assert df.iloc[0]["income"] == 1000


def test_selects_sheet_by_name(tmp_path):
    path = _write(
        tmp_path,
        [["a"], [1]],
        sheets={"Filings": [["client", "income"], ["ACME", 500]]},
    )
    df = _read_xlsx(path, {"sheet_name": "Filings"})
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["income"] == 500


def test_selects_sheet_by_position(tmp_path):
    path = _write(
        tmp_path,
        [["a"], [1]],
        sheets={"Second": [["client"], ["ACME"]]},
    )
    df = _read_xlsx(path, {"sheet_name": 1})
    assert list(df.columns) == ["client"]


def test_header_row_skips_banner_rows(tmp_path):
    path = _write(tmp_path, [
        ["Venezuela lobbying — Q1 2026"],
        ["Source: Senate LDA"],
        [],
        ["client", "income"],
        ["ACME", 1000],
    ])
    df = _read_xlsx(path, {"header_row": 3})
    assert list(df.columns) == ["client", "income"]
    assert len(df) == 1
    assert df.iloc[0]["client"] == "ACME"


def test_first_column_skips_leading_columns(tmp_path):
    path = _write(tmp_path, [
        ["note", "client", "income"],
        ["x", "ACME", 1000],
    ])
    df = _read_xlsx(path, {"first_column": 1})
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["client"] == "ACME"


def test_header_row_and_first_column_combine(tmp_path):
    path = _write(tmp_path, [
        ["Banner", "", ""],
        ["note", "client", "income"],
        ["x", "ACME", 1000],
    ])
    df = _read_xlsx(path, {"header_row": 1, "first_column": 1})
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["income"] == 1000


def test_unknown_sheet_name_raises_naming_the_sheet(tmp_path):
    path = _write(tmp_path, [["client"], ["ACME"]])
    with pytest.raises(ValueError, match="Nope"):
        _read_xlsx(path, {"sheet_name": "Nope"})


def test_authoring_surfaces_advertise_xlsx():
    from app.models import NODE_TYPES
    from app.models.stage import Connector

    params_description = Connector.model_fields["params"].description or ""
    assert "xlsx" in params_description

    notes = NODE_TYPES["input_data"]["notes"]
    assert "xlsx" in notes
    for param in ("sheet_name", "header_row", "first_column"):
        assert param in notes, f"{param} not advertised to the authoring agent"
