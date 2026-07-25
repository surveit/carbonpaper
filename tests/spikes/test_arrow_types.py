"""Spike (issue #194): the column-type vocabulary → Arrow, including the
json shapes, which are where the mapping is least obvious."""
from __future__ import annotations

import pyarrow as pa
import pytest

from app.models import Column, TableSchema
from app.models.schema import SCALAR_COLUMN_TYPES
from app.spikes.substrate.arrow_types import arrow_schema_for, arrow_type_for


def _column(**kwargs) -> Column:
    return Column.model_validate({"name": "c", **kwargs})


def test_every_scalar_in_the_vocabulary_has_an_arrow_type():
    """Totality is the property that matters: a type with no Arrow mapping is a
    column the substrate cannot carry."""
    for column_type in sorted(SCALAR_COLUMN_TYPES):
        assert isinstance(arrow_type_for(_column(type=column_type)), pa.DataType)


def test_list_types_nest():
    assert arrow_type_for(_column(type="list[str]")) == pa.list_(pa.string())
    assert arrow_type_for(_column(type="list[list[int]]")) == pa.list_(pa.list_(pa.int64()))


def test_a_json_column_with_declared_fields_becomes_a_struct():
    column = _column(type="json", fields=[
        {"name": "iso", "type": "str"},
        {"name": "population", "type": "int"},
    ])
    assert arrow_type_for(column) == pa.struct([
        pa.field("iso", pa.string(), nullable=True),
        pa.field("population", pa.int64(), nullable=True),
    ])


def test_an_open_json_column_becomes_a_map():
    assert arrow_type_for(_column(type="json", value_type="str")) == pa.map_(
        pa.string(), pa.string()
    )


def test_list_of_json_nests_the_struct():
    column = _column(type="list[json]", fields=[{"name": "iso", "type": "str"}])
    assert arrow_type_for(column) == pa.list_(
        pa.struct([pa.field("iso", pa.string(), nullable=True)])
    )


def test_nullability_travels_into_the_arrow_field():
    """Arrow records nullability in the schema, so it is a property of the data
    rather than only of a downstream check."""
    schema = arrow_schema_for(TableSchema.model_validate({
        "columns": [
            {"name": "id", "type": "int", "nullable": False},
            {"name": "note", "type": "str", "nullable": True},
        ]
    }))
    assert schema.field("id").nullable is False
    assert schema.field("note").nullable is True


def test_a_json_leaf_with_no_declared_shape_is_refused():
    """`Column._json_shape` forces `fields` or `value_type` on json/list[json],
    but not on a deeper nesting — so the absence is reported here rather than
    becoming an untyped blob."""
    column = Column.model_validate({"name": "c", "type": "list[list[json]]"})
    with pytest.raises(ValueError, match="must declare 'fields' or 'value_type'"):
        arrow_type_for(column)


def test_a_struct_value_arrives_as_a_dict_and_a_map_as_pairs():
    """The one ergonomics regression the spike found: an open json column is an
    Arrow map, which reads back as (key, value) pairs, not a dict."""
    schema = arrow_schema_for(TableSchema.model_validate({
        "columns": [
            {"name": "fixed", "type": "json", "fields": [{"name": "iso", "type": "str"}]},
            {"name": "open", "type": "json", "value_type": "str"},
        ]
    }))
    table = pa.Table.from_pylist(
        [{"fixed": {"iso": "FR"}, "open": [("a", "1")]}], schema=schema
    )
    row = table.to_pylist()[0]
    assert row["fixed"] == {"iso": "FR"}
    assert row["open"] == [("a", "1")]
    assert dict(row["open"]) == {"a": "1"}
