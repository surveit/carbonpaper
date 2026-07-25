"""Spike (issue #194): the app's column-type vocabulary → an Arrow type.

This is the glue layer the issue's table names. Both halves of the spike need
it and for the same reason: an Arrow type is the *only* thing that lets an
empty result keep its columns. `pd.DataFrame([])` has no columns because a
numpy-backed frame's schema is inferred from its rows; an Arrow table's schema
is declared up front, so zero rows and three columns is a representable state.

The mapping is total over the vocabulary in `app.models.schema` — every
`is_valid_column_type` string has an Arrow type — because a `json`/`list[json]`
column is already forced to declare exactly one of `fields` (a fixed key set →
`struct`) or `value_type` (an open string→scalar map → `map`). That declaration
is what makes the mapping possible at all; a substrate with no schema for JSON
is precisely what pandas' `object` dtype is.

Two shapes to know about when reading the row boundary (`arrow_rows.py`):

- a `map` value arrives in Python as a list of ``(key, value)`` tuples, not a
  dict — Arrow's map type is an association list. A transform that indexes an
  open json column by key needs `dict(...)` around it. This is the one real
  ergonomics regression the spike found.
- a `struct` value arrives as a plain dict with every declared key present
  (absent keys are `None`), which is *better* than today's numpy-pandas
  behaviour, where a missing key is simply absent.
"""
from __future__ import annotations

import re

import pyarrow as pa

from app.models import Column, TableSchema
from app.models.schema import JSON_COLUMN_TYPE, SCALAR_COLUMN_TYPES

# The app's scalar vocabulary → Arrow. `datetime` is microsecond-precision
# because that is what parquet round-trips through pandas today; `date` is
# date32 (days), the narrowest type that holds a calendar date without
# implying a time zone.
_SCALAR_ARROW_TYPES: dict[str, pa.DataType] = {
    "str": pa.string(),
    "int": pa.int64(),
    "float": pa.float64(),
    "bool": pa.bool_(),
    "datetime": pa.timestamp("us"),
    "date": pa.date32(),
}

_LIST_TYPE_RE = re.compile(r"^list\[(.+)\]$")


def arrow_type_for(column: Column) -> pa.DataType:
    """The Arrow type for one declared column, recursing through `list[...]`.

    Raises `ValueError` for a type outside `app.models.schema`'s vocabulary, or
    for a `json` leaf that declares neither `fields` nor `value_type` — the
    model forbids both cases, so reaching either means the column did not come
    from a validated `Column`.
    """
    return _arrow_type_for_type_string(column, column.type)


def arrow_schema_for(schema: TableSchema) -> pa.Schema:
    """The Arrow schema a frame declaring `schema` should carry — including
    each column's nullability, which Arrow records in the field itself rather
    than leaving to a downstream check."""
    return pa.schema([
        pa.field(column.name, arrow_type_for(column), nullable=column.nullable)
        for column in schema.columns
    ])


def _arrow_type_for_type_string(column: Column, type_string: str) -> pa.DataType:
    scalar = _SCALAR_ARROW_TYPES.get(type_string)
    if scalar is not None:
        return scalar
    if type_string == JSON_COLUMN_TYPE:
        return _arrow_json_type(column)
    match = _LIST_TYPE_RE.match(type_string)
    if match:
        return pa.list_(_arrow_type_for_type_string(column, match.group(1).strip()))
    raise ValueError(
        f"column {column.name!r}: no Arrow type for declared type {type_string!r}"
    )


def _arrow_json_type(column: Column) -> pa.DataType:
    """A `json` leaf: a struct of its declared `fields`, or a string→scalar map
    of its declared `value_type`. `Column._json_shape` guarantees exactly one
    is set on a json/list[json] column; a deeper nesting (`list[list[json]]`)
    escapes that validator, so the absence is reported here rather than
    silently becoming an untyped blob."""
    if column.fields is not None:
        return pa.struct([
            pa.field(field.name, arrow_type_for(field), nullable=field.nullable)
            for field in column.fields
        ])
    if column.value_type is not None:
        if column.value_type not in SCALAR_COLUMN_TYPES:
            raise ValueError(
                f"column {column.name!r}: value_type {column.value_type!r} is not a scalar"
            )
        return pa.map_(pa.string(), _SCALAR_ARROW_TYPES[column.value_type])
    raise ValueError(
        f"column {column.name!r}: a json column must declare 'fields' or 'value_type' "
        "for its Arrow type to be knowable"
    )
