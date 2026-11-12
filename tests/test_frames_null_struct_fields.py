from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.core.frames import (
    frame_to_table,
    read_frame_file,
    read_frame_file_for_display,
    table_to_frame,
    type_null_struct_fields,
    write_frame_file,
)

_SPANS = [[{"quote": "a", "prefix": None}, {"quote": "b", "prefix": None}],
          [{"quote": "c", "prefix": None}]]
_NULLS = [[None, None], [None]]

_FILE_READERS = [read_frame_file, read_frame_file_for_display]


def _pages_table() -> pa.Table:
    return pa.table({"case": ["x", "y"], "pages": _SPANS})


def _nulls_table() -> pa.Table:
    return pa.table({"tags": pa.array(_NULLS, type=pa.list_(pa.null()))})


def test_an_all_null_field_inside_a_list_of_structs_becomes_a_string_field() -> None:
    typed = type_null_struct_fields(_pages_table())

    assert typed.schema.field("pages").type == pa.list_(
        pa.struct([("quote", pa.string()), ("prefix", pa.string())]))
    assert typed.column("pages").to_pylist() == _SPANS


def test_a_table_with_no_null_field_is_returned_as_it_is() -> None:
    table = pa.table({"case": ["x"], "n": [None], "pages": [[{"quote": "a"}]]})
    assert type_null_struct_fields(table) is table


@pytest.mark.parametrize("read", _FILE_READERS)
def test_a_list_of_structs_with_an_all_null_field_reads_back_from_parquet_unchanged(
    read: Callable[[Path], pd.DataFrame], tmp_path: Path,
) -> None:
    path = tmp_path / "pages.parquet"
    pq.write_table(_pages_table(), path)

    assert read(path)["pages"].tolist() == _SPANS


@pytest.mark.parametrize("read", _FILE_READERS)
def test_a_list_of_nulls_reads_back_from_parquet_unchanged(
    read: Callable[[Path], pd.DataFrame], tmp_path: Path,
) -> None:
    path = tmp_path / "tags.parquet"
    pq.write_table(_nulls_table(), path)

    assert read(path)["tags"].tolist() == _NULLS


def test_a_list_of_structs_with_an_all_null_field_converts_to_pandas_unchanged() -> None:
    assert table_to_frame(_pages_table())["pages"].tolist() == _SPANS


def test_a_list_of_nulls_converts_to_pandas_unchanged() -> None:
    assert table_to_frame(_nulls_table())["tags"].tolist() == _NULLS


def test_a_frame_file_with_such_a_column_keeps_its_other_columns_pandas_types(
    tmp_path: Path,
) -> None:
    frame = pd.DataFrame({"n": pd.array([1, None], dtype="Int64"), "pages": _SPANS})
    path = tmp_path / "pages.parquet"
    write_frame_file(frame, path)

    pd.testing.assert_series_equal(read_frame_file(path)["n"], frame["n"])


def test_a_merge_reorders_rows_of_such_a_column_and_writes_a_valid_table() -> None:
    frame = table_to_frame(_pages_table())
    reference = pd.DataFrame({"case": ["y", "x"], "n": [1, 2]})

    joined = frame_to_table(frame.merge(reference, on="case", how="left"))

    joined.validate(full=True)
    assert joined.column("pages").to_pylist() == _SPANS
