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
from conftest import make_run_context


def _stage(path: Path, columns: list[dict], **params: object) -> Stage:
    return parse_stage({
        "id": "load", "name": "load", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), **params}},
        "output_schema": {"columns": columns},
    })


def _read(path: Path, columns: list[dict], **params: object) -> pd.DataFrame:
    return read_input_data(_stage(path, columns, **params), ctx=make_run_context())


def _csv(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "in.csv"
    path.write_text(text, encoding="utf-8")
    return path


# ── The silent-data-loss case ────────────────────────────────────────────────

def test_zero_padded_ids_declared_str_survive_the_read(tmp_path):
    path = _csv(tmp_path, "id,n\n002,5\n017,6\n")
    df = _read(path, [{"name": "id", "type": "str"}, {"name": "n", "type": "int"}])
    assert list(df["id"]) == ["002", "017"]


def test_bare_read_would_have_lost_them(tmp_path):
    # Pins WHY the fix is needed: the same file, read the way the handler read
    # it before, silently turns the identifiers into integers.
    path = _csv(tmp_path, "id,n\n002,5\n017,6\n")
    assert list(pd.read_csv(path)["id"]) == [2, 17]


def test_all_numeric_str_column_stays_str(tmp_path):
    path = _csv(tmp_path, "zip,city\n90210,Beverly Hills\n02134,Boston\n")
    df = _read(path, [{"name": "zip", "type": "str"}, {"name": "city", "type": "str"}])
    assert df["zip"].dtype == object or pd.api.types.is_string_dtype(df["zip"])
    assert list(df["zip"]) == ["90210", "02134"]


# ── Declared dates ───────────────────────────────────────────────────────────

def test_declared_date_column_parses_without_any_param(tmp_path):
    path = _csv(tmp_path, "filed_on,client\n2026-01-15,ACME\n2026-02-01,BETA\n")
    df = _read(path, [{"name": "filed_on", "type": "date"},
                      {"name": "client", "type": "str"}])
    assert pd.api.types.is_datetime64_any_dtype(df["filed_on"])
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-01-15")


def test_declared_datetime_column_parses_without_any_param(tmp_path):
    path = _csv(tmp_path, "seen_at\n2026-01-15T09:30:00\n")
    df = _read(path, [{"name": "seen_at", "type": "datetime"}])
    assert df["seen_at"].iloc[0] == pd.Timestamp("2026-01-15 09:30:00")


def test_compact_yyyymmdd_date_is_not_read_as_a_number(tmp_path):
    # Without pinning the column to str first, pandas infers int64 and
    # pd.to_datetime reads the digits as NANOSECONDS since the epoch — a 1970
    # timestamp, not a 2026 date. The str pin is what keeps this honest.
    path = _csv(tmp_path, "filed_on\n20260115\n")
    df = _read(path, [{"name": "filed_on", "type": "date"}])
    assert df["filed_on"].iloc[0] == pd.Timestamp("2026-01-15")


def test_explicit_parse_dates_still_works(tmp_path):
    # The param names a column the schema calls `str`; the authored param wins.
    path = _csv(tmp_path, "when,note\n2026-03-04,x\n")
    df = _read(path, [{"name": "when", "type": "str"}, {"name": "note", "type": "str"}],
               parse_dates=["when"])
    assert pd.api.types.is_datetime64_any_dtype(df["when"])
    assert df["when"].iloc[0] == pd.Timestamp("2026-03-04")


def test_explicit_parse_dates_and_a_declared_date_column_coexist(tmp_path):
    path = _csv(tmp_path, "a,b\n2026-03-04,2026-05-06\n")
    df = _read(path, [{"name": "a", "type": "str"}, {"name": "b", "type": "date"}],
               parse_dates=["a"])
    assert df["a"].iloc[0] == pd.Timestamp("2026-03-04")
    assert df["b"].iloc[0] == pd.Timestamp("2026-05-06")


def test_unparseable_declared_date_coerces_to_nat_rather_than_raising(tmp_path):
    # Same failure mode the authored `parse_dates` param has always had.
    path = _csv(tmp_path, "filed_on\nnot a date\n")
    df = _read(path, [{"name": "filed_on", "type": "date"}])
    assert pd.isna(df["filed_on"].iloc[0])


def test_parse_dates_naming_an_absent_column_is_still_a_no_op(tmp_path):
    path = _csv(tmp_path, "a\n1\n")
    df = _read(path, [{"name": "a", "type": "int"}], parse_dates=["ghost"])
    assert list(df.columns) == ["a"]


# ── Types the reader must leave alone ────────────────────────────────────────

def test_int_float_and_bool_columns_are_unaffected(tmp_path):
    path = _csv(tmp_path, "n,amount,flag\n5,1.5,True\n6,2.5,False\n")
    df = _read(path, [{"name": "n", "type": "int"},
                      {"name": "amount", "type": "float"},
                      {"name": "flag", "type": "bool"}])
    assert pd.api.types.is_integer_dtype(df["n"])
    assert pd.api.types.is_float_dtype(df["amount"])
    assert pd.api.types.is_bool_dtype(df["flag"])
    assert list(df["n"]) == [5, 6]


def test_zero_padded_column_declared_int_is_still_read_as_int(tmp_path):
    # The declaration is what the reader follows — it does not second-guess it.
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "int"}])
    assert list(df["id"]) == [2]


# ── list_columns keeps working ───────────────────────────────────────────────

def test_list_columns_splitting_is_unchanged(tmp_path):
    path = _csv(tmp_path, 'name,tags\nACME,"[a, b]"\nBETA,[c]\n')
    df = _read(path, [{"name": "name", "type": "str"},
                      {"name": "tags", "type": "list[str]"}],
               list_columns=["tags"])
    assert list(df["tags"]) == [["a", "b"], ["c"]]


def test_list_column_of_numeric_looking_values_keeps_its_zero_padding(tmp_path):
    # A one-element list cell like `002` is exactly the case a bare read would
    # have turned into the integer 2 before _parse_list_cell ever saw it.
    path = _csv(tmp_path, "codes\n002\n017\n")
    df = _read(path, [{"name": "codes", "type": "list[str]"}], list_columns=["codes"])
    assert list(df["codes"]) == [["002"], ["017"]]


def test_empty_list_cell_still_parses_to_the_empty_list(tmp_path):
    path = _csv(tmp_path, "name,tags\nACME,\n")
    df = _read(path, [{"name": "name", "type": "str"},
                      {"name": "tags", "type": "list[str]"}],
               list_columns=["tags"])
    assert list(df["tags"]) == [[]]


def test_declared_list_column_without_list_columns_param_is_left_as_text(tmp_path):
    path = _csv(tmp_path, 'tags\n"[a, b]"\n')
    df = _read(path, [{"name": "tags", "type": "list[str]"}])
    assert df["tags"].iloc[0] == "[a, b]"


# ── Precedence and fallbacks ─────────────────────────────────────────────────

def test_explicit_dtype_param_wins_over_the_pinned_one(tmp_path):
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "str"}], dtype={"id": "int64"})
    assert list(df["id"]) == [2]


def test_a_declared_column_absent_from_the_file_is_not_an_error(tmp_path):
    path = _csv(tmp_path, "id\n002\n")
    df = _read(path, [{"name": "id", "type": "str"}, {"name": "ghost", "type": "str"}])
    assert list(df.columns) == ["id"]
    assert list(df["id"]) == ["002"]


def test_missing_output_schema_falls_back_to_plain_inference(tmp_path):
    # Stage validation requires output_schema on input_data, so this shape can
    # only arrive off-model; the reader must degrade, not raise.
    path = _csv(tmp_path, "id\n002\n")
    stage = _stage(path, [{"name": "id", "type": "str"}])
    stage = stage.model_copy(update={"output_schema": None})
    df = read_input_data(stage, ctx=make_run_context())
    assert list(df["id"]) == [2]


# ── Formats other than csv ───────────────────────────────────────────────────

def test_json_lines_str_column_keeps_its_zero_padding(tmp_path):
    # read_json infers just as read_csv does: a JSON *string* "002" still comes
    # back as the integer 2 unless the column is pinned.
    path = tmp_path / "in.jsonl"
    path.write_text('{"id": "002", "n": 5}\n{"id": "017", "n": 6}\n', encoding="utf-8")
    df = _read(path, [{"name": "id", "type": "str"}, {"name": "n", "type": "int"}],
               format="json")
    assert list(df["id"]) == ["002", "017"]
    assert list(df["n"]) == [5, 6]


def test_json_lines_list_column_arrives_as_a_real_list(tmp_path):
    # jsonl carries real JSON types, so a list column must NOT be pinned to str
    # — _parse_list_cell passes a genuine list straight through, and
    # stringifying it first would smuggle in the repr's quotes.
    path = tmp_path / "in.jsonl"
    path.write_text('{"tags": ["a", "b"]}\n', encoding="utf-8")
    df = _read(path, [{"name": "tags", "type": "list[str]"}],
               format="json", list_columns=["tags"])
    assert list(df["tags"]) == [["a", "b"]]


def test_parquet_types_are_taken_from_the_file_not_the_declaration(tmp_path):
    # parquet stores real types; the reader adds no dtype of its own.
    path = tmp_path / "in.parquet"
    pd.DataFrame({"id": ["002", "017"], "n": [5, 6]}).to_parquet(path)
    df = _read(path, [{"name": "id", "type": "str"}, {"name": "n", "type": "int"}],
               format="parquet")
    assert list(df["id"]) == ["002", "017"]
    assert pd.api.types.is_integer_dtype(df["n"])


@pytest.mark.parametrize("fmt", ["parquet", "geojson", "xlsx"])
def test_typed_formats_do_not_get_a_pinned_dtype(fmt):
    from app.runtime.stages.input_data import _read_dtype
    from app.models import TableSchema

    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "str"}]})
    assert _read_dtype(schema, fmt, {}) is None
