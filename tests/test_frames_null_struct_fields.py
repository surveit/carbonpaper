from __future__ import annotations

import pandas as pd
import pyarrow as pa

from app.core.frames import frame_to_table, table_to_frame, type_null_struct_fields

_SPANS = [[{"quote": "a", "prefix": None}, {"quote": "b", "prefix": None}],
          [{"quote": "c", "prefix": None}]]


def _pages_table() -> pa.Table:
    return pa.table({"case": ["x", "y"], "pages": _SPANS})


def test_an_all_null_field_inside_a_list_of_structs_becomes_a_string_field() -> None:
    typed = type_null_struct_fields(_pages_table())

    assert typed.schema.field("pages").type == pa.list_(
        pa.struct([("quote", pa.string()), ("prefix", pa.string())]))
    assert typed.column("pages").to_pylist() == _SPANS


def test_a_table_with_no_null_field_is_returned_as_it_is() -> None:
    table = pa.table({"case": ["x"], "n": [None], "pages": [[{"quote": "a"}]]})
    assert type_null_struct_fields(table) is table


def test_a_merge_reorders_rows_of_such_a_column_and_writes_a_valid_table() -> None:
    frame = table_to_frame(type_null_struct_fields(_pages_table()))
    reference = pd.DataFrame({"case": ["y", "x"], "n": [1, 2]})

    joined = frame_to_table(frame.merge(reference, on="case", how="left"))

    joined.validate(full=True)
    assert joined.column("pages").to_pylist() == _SPANS
