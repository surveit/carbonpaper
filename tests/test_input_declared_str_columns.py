"""A column an input stage declares `str` is read as the source text, rather than
left to the reader's type inference (app/runtime/stages/input_data.py)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from app.models import Stage, parse_stage
from app.runtime.stages.input_data import read_input_data
from app.runtime.validation import validate_dataframe
from conftest import make_run_context

LDA_SAMPLE = Path(__file__).parent / "fixtures" / "lda_q1_venezuela_sample.xlsx"

# The Q1 2026 Senate LDA export as the venezuela_lda_lobbying project declares it:
# every column is the text the export carries, money included.
LDA_COLUMNS = [
    "year", "type", "date_posted", "client", "client_state", "client_country",
    "registrant", "income", "expenses", "issue_codes", "specific_issues",
    "lobbyists", "filing_uuid",
]


def _stage(path: Path, fmt: str, columns: list[dict[str, Any]], **params: Any) -> Stage:
    return parse_stage({
        "id": "load", "name": "load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(path), "format": fmt, **params}},
        "output_schema": {"columns": columns},
    })


def _read(stage: Stage) -> pd.DataFrame:
    return read_input_data(stage, ctx=make_run_context())


def _str_column(name: str) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True}


def test_csv_digits_declared_str_arrive_as_text(tmp_path):
    path = tmp_path / "filings.csv"
    path.write_text("client,year,income\nACME,2026,40000.00\n", encoding="utf-8")
    df = _read(_stage(path, "csv", [_str_column(c) for c in ("client", "year", "income")]))
    assert df.iloc[0]["year"] == "2026"
    # The text as exported, digit for digit — not 40000.0 round-tripped through float.
    assert df.iloc[0]["income"] == "40000.00"


def test_csv_column_declared_int_is_still_read_as_a_number(tmp_path):
    # Only `str` is pinned. A column declared int/float that arrives as text stays
    # a real mismatch for validation to report.
    path = tmp_path / "filings.csv"
    path.write_text("year,source_row\n2026,7\n", encoding="utf-8")
    df = _read(_stage(path, "csv", [
        _str_column("year"), {"name": "source_row", "type": "int", "nullable": False},
    ]))
    assert df.iloc[0]["source_row"] == 7
    assert pd.api.types.is_integer_dtype(df["source_row"])


def test_declared_column_absent_from_the_file_does_not_break_the_read(tmp_path):
    path = tmp_path / "filings.csv"
    path.write_text("client\nACME\n", encoding="utf-8")
    df = _read(_stage(path, "csv", [_str_column("client"), _str_column("nowhere")]))
    assert list(df.columns) == ["client"]


def test_json_lines_digits_declared_str_arrive_as_text(tmp_path):
    path = tmp_path / "filings.jsonl"
    path.write_text('{"client":"ACME","year":2026}\n', encoding="utf-8")
    df = _read(_stage(path, "json", [_str_column("client"), _str_column("year")]))
    assert df.iloc[0]["year"] == "2026"


def test_lda_export_validates_against_the_schema_that_declares_it_text():
    # The reported failure at fixture scale: read with type inference, `year` comes
    # back int64 and `income`/`expenses` float64, and the stage fails its own
    # output_schema with an OutputSchemaViolation naming those three columns.
    stage = _stage(LDA_SAMPLE, "xlsx", [_str_column(c) for c in LDA_COLUMNS],
                   sheet_name="Sheet1", header_row=0)
    df = _read(stage)
    assert stage.output_schema is not None
    report = validate_dataframe(df, stage.output_schema, stage_id="load", phase="output")
    assert report.ok, [i.message for i in report.issues if i.severity == "error"]
    assert df.iloc[0]["year"] == "2026"


def test_source_row_column_is_still_the_integer_it_is_declared():
    # source_row is added after the read, so no dtype of the source file's can
    # reach it; it stays the int the schema declares.
    stage = _stage(LDA_SAMPLE, "xlsx",
                   [_str_column(c) for c in LDA_COLUMNS]
                   + [{"name": "source_row", "type": "int", "nullable": False}],
                   sheet_name="Sheet1", header_row=0, source_row_column="source_row")
    df = _read(stage)
    assert list(df["source_row"][:3]) == [2, 3, 4]


def test_parquet_keeps_the_types_it_stores(tmp_path):
    # Parquet carries its own types, so there is nothing to infer and nothing to
    # pin: a column stored as int under a `str` declaration is a genuine
    # disagreement between file and schema, and validation reports it.
    path = tmp_path / "filings.parquet"
    pd.DataFrame({"year": [2026]}).to_parquet(path)
    stage = _stage(path, "parquet", [_str_column("year")])
    df = _read(stage)
    assert stage.output_schema is not None
    report = validate_dataframe(df, stage.output_schema, stage_id="load", phase="output")
    assert not report.ok
