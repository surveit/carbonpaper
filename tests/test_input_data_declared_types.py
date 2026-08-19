"""read_input_data honours the output_schema an input stage is required to
declare, instead of letting pandas guess (app/runtime/stages/input_data.py).
The cases that matter are where inference is silently WRONG: a zero-padded
identifier declared `str` read as int64 (`002` → `2`, no error), and a declared
date column arriving as text."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models import Stage
from app.models.stage import parse_stage
from app.runtime.stages.input_data import read_input_data
from conftest import make_run_context, place_stage


def _stage(path: Path, columns: list[dict], **params: object) -> Stage:
    return parse_stage({
        "id": "load", "description": "load", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), **params}},
        "signature": {"form": "replaces", "produces": columns},
    })


def _read(path: Path, columns: list[dict], **params: object) -> pd.DataFrame:
    return read_input_data(place_stage(_stage(path, columns, **params)), ctx=make_run_context())


def _csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "in.csv"
    path.write_text(text, encoding="utf-8")
    return path


# ── The silent-data-loss case ────────────────────────────────────────────────

def test_zero_padded_ids_declared_str_survive_the_read(tmp_path):
    path = _csv(tmp_path, "id,n\n002,5\n017,6\n")
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}, {"name": "n", "type": "int", "nullable": True}])
    assert list(df["id"]) == ["002", "017"]


def test_bare_read_would_have_lost_them(tmp_path):
    path = _csv(tmp_path, "id,n\n002,5\n017,6\n")
    assert list(pd.read_csv(path)["id"]) == [2, 17]


def test_all_numeric_str_column_stays_str(tmp_path):
    path = _csv(tmp_path, "zip,city\n90210,Beverly Hills\n02134,Boston\n")
    df = _read(path, [{"name": "zip", "type": "str", "nullable": True}, {"name": "city", "type": "str", "nullable": True}])
    assert df["zip"].dtype == object or pd.api.types.is_string_dtype(df["zip"])
    assert list(df["zip"]) == ["90210", "02134"]


def test_tab_separated_content_with_a_csv_suffix_loads(tmp_path):
    path = tmp_path / (
        "authorcomment_AND_authorAction_Justice_Climat_Lyon - Aug 18, 2026 - 10 19 50 AM.csv"
    )
    path.write_text("authorcomment\tauthorAction\nKeep\tEscalate\n", encoding="utf-8")
    df = _read(path, [
        {"name": "authorcomment", "type": "str", "nullable": True},
        {"name": "authorAction", "type": "str", "nullable": True},
    ])
    assert list(df.columns) == ["authorcomment", "authorAction"]
    assert df.to_dict(orient="records") == [
        {"authorcomment": "Keep", "authorAction": "Escalate"}
    ]


def test_tab_separated_header_may_quote_a_comma(tmp_path):
    path = _csv(tmp_path, '"author,comment"\tauthorAction\nKeep\tEscalate\n')
    df = _read(path, [
        {"name": "author,comment", "type": "str", "nullable": True},
        {"name": "authorAction", "type": "str", "nullable": True},
    ])
    assert df.to_dict(orient="records") == [
        {"author,comment": "Keep", "authorAction": "Escalate"}
    ]


def test_tsv_format_loads_a_tsv_file(tmp_path):
    path = tmp_path / "in.tsv"
    path.write_text("id\tnote\n002\thello, world\n", encoding="utf-8")
    df = _read(path, [
        {"name": "id", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True},
    ], format="tsv")
    assert df.to_dict(orient="records") == [{"id": "002", "note": "hello, world"}]


def test_ordinary_csv_with_a_quoted_comma_is_unchanged(tmp_path):
    path = _csv(tmp_path, 'name,note\nAlice,"hello, world"\n')
    df = _read(path, [
        {"name": "name", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True},
    ])
    assert df.to_dict(orient="records") == [{"name": "Alice", "note": "hello, world"}]


def test_windows_1252_csv_with_distinct_punctuation_loads(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes('name,note\nAndré,“Lyon”\n'.encode("windows-1252"))
    df = _read(path, [
        {"name": "name", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True},
    ])
    assert df.to_dict(orient="records") == [{"name": "André", "note": "“Lyon”"}]


def test_utf8_with_the_same_characters_is_unchanged(tmp_path):
    path = _csv(tmp_path, 'name,note\nAndré,“Lyon”\n')
    df = _read(path, [
        {"name": "name", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True},
    ])
    assert df.to_dict(orient="records") == [{"name": "André", "note": "“Lyon”"}]


def test_windows_1252_after_the_sample_loads(tmp_path):
    path = tmp_path / "in.csv"
    note = "a" * 65_536 + "“Lyon”"
    path.write_bytes(("name,note\nAlice," + note + "\n").encode("windows-1252"))
    df = _read(path, [
        {"name": "name", "type": "str", "nullable": True},
        {"name": "note", "type": "str", "nullable": True},
    ])
    assert df.to_dict(orient="records") == [{"name": "Alice", "note": note}]


def test_utf8_character_may_cross_the_sample_boundary(tmp_path):
    path = tmp_path / "in.csv"
    scan_bytes = 65_536
    value = "a" * (scan_bytes - len(b"name\n") - 1) + "€"
    path.write_text("name\n" + value + "\n", encoding="utf-8")
    df = _read(path, [{"name": "name", "type": "str", "nullable": True}])
    assert df.to_dict(orient="records") == [{"name": value}]


def test_undefined_windows_1252_byte_fails_loudly(tmp_path):
    path = tmp_path / "in.csv"
    path.write_bytes(b"name,note\nAndr\xe9,\x93bad\x81\x94\n")
    with pytest.raises(UnicodeDecodeError):
        _read(path, [
            {"name": "name", "type": "str", "nullable": True},
            {"name": "note", "type": "str", "nullable": True},
        ])


def test_an_ambiguous_mixed_delimiter_header_fails_loudly(tmp_path):
    path = _csv(tmp_path, "left,right\tthird\nvalue\n")
    with pytest.raises(ValueError, match="cannot distinguish comma-separated from tab-separated"):
        _read(path, [
            {"name": "left", "type": "str", "nullable": True},
            {"name": "right", "type": "str", "nullable": True},
            {"name": "third", "type": "str", "nullable": True},
        ])


# ── Declared dates ───────────────────────────────────────────────────────────

def test_declared_date_column_parses_without_any_param(tmp_path):
    path = _csv(tmp_path, "filed_on,client\n2026-01-15,ACME\n2026-02-01,BETA\n")
    df = _read(path, [{"name": "filed_on", "type": "date", "nullable": True},
                      {"name": "client", "type": "str", "nullable": True}])
    assert pd.api.types.is_datetime64_any_dtype(df["filed_on"])
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-01-15")


def test_declared_datetime_column_parses_without_any_param(tmp_path):
    path = _csv(tmp_path, "seen_at\n2026-01-15T09:30:00\n")
    df = _read(path, [{"name": "seen_at", "type": "datetime", "nullable": True}])
    assert df["seen_at"].iloc[0] == pd.Timestamp("2026-01-15 09:30:00")


def test_compact_yyyymmdd_date_is_not_read_as_a_number(tmp_path):
    # Unpinned, pandas infers int64 and to_datetime reads the digits as nanoseconds: a 1970 date.
    path = _csv(tmp_path, "filed_on\n20260115\n")
    df = _read(path, [{"name": "filed_on", "type": "date", "nullable": True}])
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-01-15")


def test_explicit_parse_dates_still_works(tmp_path):
    path = _csv(tmp_path, "when,note\n2026-03-04,x\n")
    df = _read(path, [{"name": "when", "type": "str", "nullable": True}, {"name": "note", "type": "str", "nullable": True}],
               parse_dates=["when"])
    assert pd.api.types.is_datetime64_any_dtype(df["when"])
    assert df["when"].iloc[0] == pd.Timestamp("2026-03-04")


def test_explicit_parse_dates_and_a_declared_date_column_coexist(tmp_path):
    path = _csv(tmp_path, "a,b\n2026-03-04,2026-05-06\n")
    df = _read(path, [{"name": "a", "type": "str", "nullable": True}, {"name": "b", "type": "date", "nullable": True}],
               parse_dates=["a"])
    assert df["a"].iloc[0] == pd.Timestamp("2026-03-04")
    assert df["b"].iloc[0] == pd.Timestamp("2026-05-06")


def test_unparseable_declared_date_coerces_to_nat_rather_than_raising(tmp_path):
    path = _csv(tmp_path, "filed_on\nnot a date\n")
    df = _read(path, [{"name": "filed_on", "type": "date", "nullable": True}])
    assert pd.isna(df["filed_on"].iloc[0])


def test_parse_dates_naming_an_absent_column_is_still_a_no_op(tmp_path):
    path = _csv(tmp_path, "a\n1\n")
    df = _read(path, [{"name": "a", "type": "int", "nullable": True}], parse_dates=["ghost"])
    assert list(df.columns) == ["a"]


# ── Types the reader must leave alone ────────────────────────────────────────

def test_int_float_and_bool_columns_are_unaffected(tmp_path):
    path = _csv(tmp_path, "n,amount,flag\n5,1.5,True\n6,2.5,False\n")
    df = _read(path, [{"name": "n", "type": "int", "nullable": True},
                      {"name": "amount", "type": "float", "nullable": True},
                      {"name": "flag", "type": "bool", "nullable": True}])
    assert pd.api.types.is_integer_dtype(df["n"])
    assert pd.api.types.is_float_dtype(df["amount"])
    assert pd.api.types.is_bool_dtype(df["flag"])
    assert list(df["n"]) == [5, 6]


def test_zero_padded_column_declared_int_is_still_read_as_int(tmp_path):
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "int", "nullable": True}])
    assert list(df["id"]) == [2]


# ── list_columns keeps working ───────────────────────────────────────────────

def test_list_columns_splitting_is_unchanged(tmp_path):
    path = _csv(tmp_path, 'name,tags\nACME,"[a, b]"\nBETA,[c]\n')
    df = _read(path, [{"name": "name", "type": "str", "nullable": True},
                      {"name": "tags", "type": "list[str]", "nullable": True}],
               list_columns=["tags"])
    assert list(df["tags"]) == [["a", "b"], ["c"]]


def test_list_column_of_numeric_looking_values_keeps_its_zero_padding(tmp_path):
    # A bare read turns `002` into 2 before _parse_list_cell ever sees it.
    path = _csv(tmp_path, "codes\n002\n017\n")
    df = _read(path, [{"name": "codes", "type": "list[str]", "nullable": True}], list_columns=["codes"])
    assert list(df["codes"]) == [["002"], ["017"]]


def test_empty_list_cell_still_parses_to_the_empty_list(tmp_path):
    path = _csv(tmp_path, "name,tags\nACME,\n")
    df = _read(path, [{"name": "name", "type": "str", "nullable": True},
                      {"name": "tags", "type": "list[str]", "nullable": True}],
               list_columns=["tags"])
    assert list(df["tags"]) == [[]]


def test_declared_list_column_without_list_columns_param_is_left_as_text(tmp_path):
    path = _csv(tmp_path, 'tags\n"[a, b]"\n')
    df = _read(path, [{"name": "tags", "type": "list[str]", "nullable": True}])
    assert df["tags"].iloc[0] == "[a, b]"


# ── Precedence and fallbacks ─────────────────────────────────────────────────

def test_explicit_dtype_param_wins_over_the_pinned_one(tmp_path):
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}], dtype={"id": "int64"})
    assert list(df["id"]) == [2]


def test_a_declared_column_absent_from_the_file_is_not_an_error(tmp_path):
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}, {"name": "ghost", "type": "str", "nullable": True}])
    assert list(df.columns) == ["id"]
    assert list(df["id"]) == ["002"]


def test_missing_output_schema_falls_back_to_plain_inference(tmp_path):
    # Validation forbids an empty `produces`, so this shape can only arrive off-model.
    path = _csv(tmp_path, "id\n002\n")
    stage = _stage(path, [{"name": "id", "type": "str", "nullable": True}])
    stage = stage.model_copy(
        update={"signature": stage.signature.model_copy(update={"produces": []})})
    df = read_input_data(place_stage(stage), ctx=make_run_context())
    assert list(df["id"]) == [2]


# ── Formats other than csv ───────────────────────────────────────────────────

def test_json_lines_str_column_keeps_its_zero_padding(tmp_path):
    # read_json infers too: a JSON *string* "002" comes back as 2 unless the column is pinned.
    path = tmp_path / "in.jsonl"
    path.write_text('{"id": "002", "n": 5}\n{"id": "017", "n": 6}\n', encoding="utf-8")
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}, {"name": "n", "type": "int", "nullable": True}],
               format="json")
    assert list(df["id"]) == ["002", "017"]
    assert list(df["n"]) == [5, 6]


def test_json_lines_list_column_arrives_as_a_real_list(tmp_path):
    # jsonl carries real types: pinning a list column to str smuggles in the repr's quotes.
    path = tmp_path / "in.jsonl"
    path.write_text('{"tags": ["a", "b"]}\n', encoding="utf-8")
    df = _read(path, [{"name": "tags", "type": "list[str]", "nullable": True}],
               format="json", list_columns=["tags"])
    assert list(df["tags"]) == [["a", "b"]]


def test_parquet_types_are_taken_from_the_file_not_the_declaration(tmp_path):
    path = tmp_path / "in.parquet"
    pd.DataFrame({"id": ["002", "017"], "n": [5, 6]}).to_parquet(path)
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}, {"name": "n", "type": "int", "nullable": True}],
               format="parquet")
    assert list(df["id"]) == ["002", "017"]
    assert pd.api.types.is_integer_dtype(df["n"])


@pytest.mark.parametrize("fmt", ["parquet", "geojson"])
def test_typed_formats_do_not_get_a_pinned_dtype(fmt):
    from app.runtime.stages.input_data import _read_dtype
    from app.models import TableSchema

    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "str", "nullable": True}]})
    assert _read_dtype(schema, fmt, {}) is None


# ── xlsx: a workbook types its cells, pd.read_excel re-guesses them ──────────

def _xlsx(tmp_path: Path, frame: pd.DataFrame) -> Path:
    path = tmp_path / "book.xlsx"
    frame.to_excel(path, index=False)
    return path


def _xlsx_cells(tmp_path: Path, header: list[str], rows: list[list[object]],
                text_columns: set[str]) -> Path:
    # to_excel cannot write a TEXT cell holding a numeric-looking value; data_type='s' can.
    import openpyxl

    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Sheet1"
    sheet.append(header)
    for values in rows:
        sheet.append(values)
    text_at = [i for i, name in enumerate(header) if name in text_columns]
    for cells in sheet.iter_rows(min_row=2):
        for i in text_at:
            if cells[i].value is not None:
                cells[i].data_type = "s"
    path = tmp_path / "cells.xlsx"
    book.save(path)
    return path


def test_an_xlsx_text_cell_declared_str_keeps_its_zero_padding(tmp_path):
    # Coercing back after the read cannot recover the padding; it has to be pinned at the read.
    path = _xlsx_cells(tmp_path, ["id"], [["002"], ["017"]], {"id"})
    df = _read(path, [{"name": "id", "type": "str", "nullable": True}], format="xlsx")
    assert list(df["id"]) == ["002", "017"]


def test_an_xlsx_text_cell_declared_str_keeps_the_digits_it_was_written_with(tmp_path):
    # Read as float, '40000.00' renders back as '40000', and a long identifier loses digits.
    path = _xlsx_cells(
        tmp_path, ["income", "big_id"],
        [["40000.00", "00123456789012345678901"]], {"income", "big_id"})
    df = _read(path, [{"name": "income", "type": "str", "nullable": True},
                      {"name": "big_id", "type": "str", "nullable": True}], format="xlsx")
    assert df["income"].iloc[0] == "40000.00"
    assert df["big_id"].iloc[0] == "00123456789012345678901"


def test_a_real_excel_date_declared_date_survives_the_str_pin(tmp_path):
    path = _xlsx(tmp_path, pd.DataFrame({"filed_on": [pd.Timestamp("2026-04-02")]}))
    df = _read(path, [{"name": "filed_on", "type": "date", "nullable": True}], format="xlsx")
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-04-02")


def test_a_compact_yyyymmdd_xlsx_cell_declared_date_is_not_read_as_a_number(tmp_path):
    path = _xlsx(tmp_path, pd.DataFrame({"filed_on": [20260115]}))
    df = _read(path, [{"name": "filed_on", "type": "date", "nullable": True}], format="xlsx")
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-01-15")


def test_an_xlsx_numeric_cell_declared_str_is_read_as_text(tmp_path):
    path = _xlsx(tmp_path, pd.DataFrame({"code": [2026, None]}))
    df = _read(path, [{"name": "code", "type": "str", "nullable": True}], format="xlsx")
    assert df["code"].dropna().tolist() == ["2026"]


def test_an_empty_xlsx_cell_declared_str_stays_null(tmp_path):
    """str(nan) is the text 'nan' — a blank must not become one."""
    path = _xlsx(tmp_path, pd.DataFrame({"code": [2026, None], "keep": ["a", "b"]}))
    df = _read(path, [{"name": "code", "type": "str", "nullable": True},
                      {"name": "keep", "type": "str", "nullable": False}], format="xlsx")
    assert df["code"].tolist()[0] == "2026"
    assert int(df["code"].isna().sum()) == 1


def test_an_xlsx_column_declared_int_is_left_alone(tmp_path):
    path = _xlsx(tmp_path, pd.DataFrame({"n": [7]}))
    df = _read(path, [{"name": "n", "type": "int", "nullable": False}], format="xlsx")
    assert df["n"].tolist() == [7]
