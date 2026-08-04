"""_read_xlsx maps connector params onto pd.read_excel (app/runtime/stages/input_data.py)."""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest
from pydantic import ValidationError

from app.models.stages.input_data import FileFormat, XlsxReadParams
from app.runtime.stages.input_data import _read_xlsx


def _params(**kwargs: object) -> XlsxReadParams:
    return XlsxReadParams.model_validate(kwargs)


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
    df = _read_xlsx(path, _params())
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
    df = _read_xlsx(path, _params(sheet_name="Filings"))
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["income"] == 500


def test_selects_sheet_by_position(tmp_path):
    path = _write(
        tmp_path,
        [["a"], [1]],
        sheets={"Second": [["client"], ["ACME"]]},
    )
    df = _read_xlsx(path, _params(sheet_name=1))
    assert list(df.columns) == ["client"]


def test_header_row_skips_banner_rows(tmp_path):
    path = _write(tmp_path, [
        ["Venezuela lobbying — Q1 2026"],
        ["Source: Senate LDA"],
        [],
        ["client", "income"],
        ["ACME", 1000],
    ])
    df = _read_xlsx(path, _params(header_row=3))
    assert list(df.columns) == ["client", "income"]
    assert len(df) == 1
    assert df.iloc[0]["client"] == "ACME"


def test_first_column_skips_leading_columns(tmp_path):
    path = _write(tmp_path, [
        ["note", "client", "income"],
        ["x", "ACME", 1000],
    ])
    df = _read_xlsx(path, _params(first_column=1))
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["client"] == "ACME"


def test_header_row_and_first_column_combine(tmp_path):
    path = _write(tmp_path, [
        ["Banner", "", ""],
        ["note", "client", "income"],
        ["x", "ACME", 1000],
    ])
    df = _read_xlsx(path, _params(header_row=1, first_column=1))
    assert list(df.columns) == ["client", "income"]
    assert df.iloc[0]["income"] == 1000


def test_unknown_sheet_name_raises_naming_the_sheet(tmp_path):
    path = _write(tmp_path, [["client"], ["ACME"]])
    with pytest.raises(ValueError, match="Nope"):
        _read_xlsx(path, _params(sheet_name="Nope"))


def test_first_column_out_of_range_raises(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValueError, match="out of range"):
        _read_xlsx(path, _params(first_column=5))


def test_first_column_negative_raises(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValueError, match="out of range"):
        _read_xlsx(path, _params(first_column=-1))


def test_first_column_default_zero_reads_whole_frame(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    df = _read_xlsx(path, _params())
    assert list(df.columns) == ["client", "income"]


@pytest.mark.parametrize("sheet", [None, ["Sheet1"]])
def test_multi_sheet_selection_rejected_up_front(sheet):
    """sheet_name is str|int (exactly one sheet); None/list (pandas' "select several
    sheets" forms) are rejected by XlsxReadParams before pd.read_excel ever runs."""
    with pytest.raises(ValidationError, match="sheet_name"):
        _params(sheet_name=sheet)


@pytest.mark.parametrize("param", ["header_row", "first_column"])
def test_non_integer_offset_param_raises(param):
    with pytest.raises(ValidationError, match=param):
        _params(**{param: 1.7})


@pytest.mark.parametrize("param", ["header_row", "first_column"])
def test_bool_offset_param_raises(param):
    with pytest.raises(ValidationError, match=param):
        _params(**{param: True})


def test_authoring_surfaces_advertise_xlsx():
    from app.models.stages.node_types import NODE_TYPES
    from app.models.stages.input_data import Connector

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
    df = _read_xlsx(LDA_SAMPLE, _params())
    assert list(df.columns) == LDA_COLUMNS
    assert len(df) == 64
    matches = df["specific_issues"].fillna("").str.contains("venezuela", case=False)
    assert int(matches.sum()) == 44


def test_source_row_column_omitted_by_default(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000], ["BETA", 2000]])
    df = _read_xlsx(path, _params())
    assert list(df.columns) == ["client", "income"]


def test_source_row_column_holds_true_sheet_row_numbers(tmp_path):
    # Each data cell carries the 1-based Excel row it lives on, so the assertion
    # is self-evident: the recorded value must equal the cell's own claim.
    path = _write(tmp_path, [["client", "row"], ["ACME", 2], ["BETA", 3]])
    df = _read_xlsx(path, _params(source_row_column="source_row"))
    assert list(df["source_row"]) == list(df["row"])


def test_source_row_column_correct_with_header_row(tmp_path):
    path = _write(tmp_path, [
        ["Venezuela lobbying — Q1 2026"],
        ["Source: Senate LDA"],
        [],
        ["client", "row"],
        ["ACME", 5],
        ["BETA", 6],
    ])
    df = _read_xlsx(path, _params(header_row=3, source_row_column="source_row"))
    assert list(df["source_row"]) == list(df["row"])


def test_source_row_column_correct_with_first_column(tmp_path):
    path = _write(tmp_path, [
        ["note", "client", "row"],
        ["x", "ACME", 2],
        ["y", "BETA", 3],
    ])
    df = _read_xlsx(path, _params(first_column=1, source_row_column="source_row"))
    assert list(df.columns) == ["client", "row", "source_row"]
    assert list(df["source_row"]) == list(df["row"])


def test_source_row_column_name_collision_raises_naming_the_column(tmp_path):
    path = _write(tmp_path, [["client", "income"], ["ACME", 1000]])
    with pytest.raises(ValueError, match="income"):
        _read_xlsx(path, _params(source_row_column="income"))


def test_source_row_column_makes_duplicate_rows_distinct(tmp_path):
    path = _write(tmp_path, [
        ["client", "income"],
        ["ACME", 1000],
        ["ACME", 1000],
    ])
    without = _read_xlsx(path, _params())
    with_col = _read_xlsx(path, _params(source_row_column="source_row"))
    assert without.iloc[0].equals(without.iloc[1])
    assert not with_col.iloc[0].equals(with_col.iloc[1])
    assert list(with_col["source_row"]) == [2, 3]
