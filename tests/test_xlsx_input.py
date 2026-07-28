"""_read_xlsx maps connector params onto pd.read_excel (app/runtime/stages/input_data.py)."""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from pydantic import ValidationError

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


def test_first_column_out_of_range_raises(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValueError, match="out of range"):
        _read_xlsx(path, {"first_column": 5})


def test_first_column_negative_raises(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValueError, match="out of range"):
        _read_xlsx(path, {"first_column": -1})


def test_first_column_default_zero_reads_whole_frame(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    df = _read_xlsx(path, {})
    assert list(df.columns) == ["client", "income"]


@pytest.mark.parametrize("sheet", [None, ["Sheet1"]])
def test_multi_sheet_selection_rejected_up_front(tmp_path, sheet):
    """sheet_name is str|int (exactly one sheet); None/list (pandas' "select several
    sheets" forms) are rejected by XlsxReadParams before pd.read_excel ever runs."""
    path = _write(tmp_path, [["client"], ["ACME"]])
    with pytest.raises(ValidationError, match="sheet_name"):
        _read_xlsx(path, {"sheet_name": sheet})


@pytest.mark.parametrize("param", ["header_row", "first_column"])
def test_non_integer_offset_param_raises(tmp_path, param):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValidationError, match=param):
        _read_xlsx(path, {param: 1.7})


@pytest.mark.parametrize("param", ["header_row", "first_column"])
def test_bool_offset_param_raises(tmp_path, param):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValidationError, match=param):
        _read_xlsx(path, {param: True})


def test_authoring_surfaces_advertise_xlsx():
    from app.models import NODE_TYPES
    from app.models.stage import Connector

    params_description = Connector.model_fields["params"].description or ""
    for fmt in FileFormat:
        assert fmt.value in params_description, f"{fmt.value} not advertised to the authoring agent"

    notes = NODE_TYPES["input_data"]["notes"]
    assert "xlsx" in notes
    for param in ("sheet_name", "header_row", "first_column"):
        assert param in notes, f"{param} not advertised to the authoring agent"


# Trimmed from the real 2026 Q1 Senate LDA quarterly export (public record): every
# row whose specific_issues mentions "venezuela" (44) plus the first 20 non-matching
# rows, real column names and real cell values throughout. Exercises real-shaped
# content at small scale — it says nothing about behavior at the full export's
# 24,797 rows.
LDA_SAMPLE = Path(__file__).parent / "fixtures" / "lda_q1_venezuela_sample.xlsx"

LDA_COLUMNS = [
    "year", "type", "date_posted", "client", "client_state", "client_country",
    "registrant", "income", "expenses", "issue_codes", "specific_issues",
    "lobbyists", "filing_uuid",
]


def test_reads_the_lda_venezuela_sample():
    df = _read_xlsx(LDA_SAMPLE, {})
    assert list(df.columns) == LDA_COLUMNS
    assert len(df) == 64
    matches = df["specific_issues"].fillna("").str.contains("venezuela", case=False)
    assert int(matches.sum()) == 44
