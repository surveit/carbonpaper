"""Spike (issue #194): the python_row_function boundary over Arrow types."""
from __future__ import annotations

import pyarrow as pa
import pytest

from app.models import TableSchema
from app.spikes.substrate.arrow_rows import (
    frame_from_rows,
    from_arrow_pandas,
    rows_from_arrow,
    run_row_function,
    to_arrow_pandas,
    to_polars,
)
from app.spikes.substrate.arrow_types import arrow_schema_for


def _schema() -> TableSchema:
    return TableSchema.model_validate({
        "columns": [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "name", "type": "str"},
            {"name": "tags", "type": "list[str]"},
            {"name": "score", "type": "float"},
        ]
    })


def _table() -> pa.Table:
    return pa.Table.from_pylist(
        [
            {"id": 1, "name": "a", "tags": ["x", "y"], "score": 1.0},
            {"id": 2, "name": None, "tags": [], "score": None},
        ],
        schema=arrow_schema_for(_schema()),
    )


def test_rows_carry_python_none_list_and_str():
    assert rows_from_arrow(_table()) == [
        {"id": 1, "name": "a", "tags": ["x", "y"], "score": 1.0},
        {"id": 2, "name": None, "tags": [], "score": None},
    ]


def test_an_authored_function_needs_no_null_guards():
    """The transform an author would naturally write — `row["name"].upper()`
    behind an `is None` check, `len(row["tags"])` — runs unguarded. On the
    numpy-pandas boundary the same function raises on `float('nan').upper()`;
    `test_null_semantics.py` runs both to show it."""

    def transform(row):
        return {
            "id": row["id"],
            "shout": row["name"].upper() if row["name"] is not None else None,
            "tag_count": len(row["tags"]),
        }

    out_schema = TableSchema.model_validate({
        "columns": [
            {"name": "id", "type": "int"},
            {"name": "shout", "type": "str"},
            {"name": "tag_count", "type": "int"},
        ]
    })
    result = run_row_function(transform, _table(), out_schema)
    assert result.to_pylist() == [
        {"id": 1, "shout": "A", "tag_count": 2},
        {"id": 2, "shout": None, "tag_count": 0},
    ]


def test_an_empty_stage_output_still_has_its_declared_columns_and_types():
    """The issue's third bug. `pd.DataFrame([])` has no columns at all; a
    declared Arrow schema makes zero-rows-three-columns representable."""
    schema = TableSchema.model_validate({
        "columns": [{"name": "id", "type": "int"}, {"name": "label", "type": "str"}]
    })
    empty = pa.Table.from_pylist([], schema=arrow_schema_for(schema))
    result = run_row_function(lambda row: row, empty, schema)
    assert result.num_rows == 0
    assert result.schema.names == ["id", "label"]
    assert result.schema.types == [pa.int64(), pa.string()]


def test_output_order_is_input_order():
    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "int"}]})
    table = pa.Table.from_pylist([{"id": i} for i in range(20)], schema=arrow_schema_for(schema))
    result = run_row_function(lambda row: {"id": row["id"]}, table, schema)
    assert result.column("id").to_pylist() == list(range(20))


def test_one_output_row_per_input_row():
    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "int"}]})
    table = pa.Table.from_pylist([{"id": 1}, {"id": 2}], schema=arrow_schema_for(schema))
    assert run_row_function(lambda row: {"id": row["id"]}, table, schema).num_rows == 2


def test_a_non_dict_return_fails_loudly_naming_the_row():
    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "int"}]})
    table = pa.Table.from_pylist([{"id": 1}], schema=arrow_schema_for(schema))
    with pytest.raises(ValueError, match="row 0"):
        run_row_function(lambda row: [row], table, schema)


def test_a_column_no_schema_declares_fails_loudly():
    schema = TableSchema.model_validate({"columns": [{"name": "id", "type": "int"}]})
    table = pa.Table.from_pylist([{"id": 1}], schema=arrow_schema_for(schema))
    with pytest.raises(ValueError, match="surprise"):
        run_row_function(lambda row: {"id": row["id"], "surprise": 1}, table, schema)


def test_a_missing_declared_column_becomes_a_null_not_a_missing_column():
    schema = TableSchema.model_validate({
        "columns": [{"name": "id", "type": "int"}, {"name": "label", "type": "str"}]
    })
    table = pa.Table.from_pylist([{"id": 1, "label": "x"}], schema=arrow_schema_for(schema))
    result = run_row_function(lambda row: {"id": row["id"]}, table, schema)
    assert result.to_pylist() == [{"id": 1, "label": None}]


def test_without_a_schema_types_are_inferred_and_an_empty_result_is_column_less():
    """What the schema actually buys: Arrow alone does not fix the empty case,
    a *declared* schema does."""
    assert frame_from_rows([], None).num_rows == 0
    assert frame_from_rows([], None).schema.names == []


# ── the two host options from the issue's table ──────────────────────────────

def test_polars_view_preserves_nulls_and_lists():
    frame = to_polars(_table())
    assert frame.rows(named=True) == rows_from_arrow(_table())
    assert str(frame.schema["tags"]) == "List(String)"


def test_arrow_backed_pandas_view_preserves_nulls_and_lists():
    frame = to_arrow_pandas(_table())
    assert str(frame["name"].dtype) == "string[pyarrow]"
    assert frame["name"].isna().tolist() == [False, True]
    assert list(frame["tags"].iloc[0]) == ["x", "y"]


def test_arrow_backed_pandas_round_trips_back_to_the_same_rows():
    assert from_arrow_pandas(to_arrow_pandas(_table())).to_pylist() == rows_from_arrow(_table())
