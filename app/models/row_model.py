"""Implementation of `TableSchema.to_pydantic_model` — one field per column.

The built Pydantic model class mirrors the schema's columns: name, scalar
type, nullability, enum vocabulary, numeric range, and description all carry
over, and a `json`/`list[json]` column with `fields` becomes a nested model,
validated recursively. Every column is a REQUIRED field: `nullable` permits a
None value, not an absent key. Unknown keys are rejected.

The public entry point is the `TableSchema.to_pydantic_model(name)` method;
this module holds the builder so the schema module stays declarative. Named
consumer: app.runtime.stages.llm_transform compiles a stage's reply spec and
hands the model to app.core.agent.agent.Agent as `target_schema`, so the reply
spec is enforced (the agent must submit a validating instance) rather than
merely described in prompt prose.
"""
from __future__ import annotations

import datetime
from typing import Any, Literal, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field, create_model

from app.models.schema import (
    Column,
    JSON_COLUMN_TYPE,
    LIST_JSON_COLUMN_TYPE,
    _LIST_RE,
)

_SCALAR_PY_TYPES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "date": datetime.date,
    "datetime": datetime.datetime,
}


def build_row_model(name: str, columns: Sequence[Column]) -> type[BaseModel]:
    field_definitions: dict[str, Any] = {}
    for column in columns:
        annotation = _annotation_for(column, parent_name=name)
        if column.nullable:
            annotation = Optional[annotation]
        field_definitions[column.name] = (annotation, _field_for(column))
    return create_model(
        name, __config__=ConfigDict(extra="forbid"), **field_definitions
    )


def _annotation_for(column: Column, parent_name: str) -> Any:
    if column.type in (JSON_COLUMN_TYPE, LIST_JSON_COLUMN_TYPE):
        inner: Any
        if column.fields is not None:
            inner = build_row_model(f"{parent_name}__{column.name}", column.fields)
        else:
            assert column.value_type is not None  # Column._json_shape enforces
            scalar_py_type: Any = _SCALAR_PY_TYPES[column.value_type]
            inner = dict[str, scalar_py_type]
        return list[inner] if column.type == LIST_JSON_COLUMN_TYPE else inner
    if column.enum is not None:
        return Literal.__getitem__(tuple(column.enum))
    return _scalar_or_list_annotation(column.type)


def _scalar_or_list_annotation(type_name: str) -> Any:
    if type_name in _SCALAR_PY_TYPES:
        return _SCALAR_PY_TYPES[type_name]
    match = _LIST_RE.match(type_name)
    if match:
        element_type: Any = _scalar_or_list_annotation(match.group(1).strip())
        return list[element_type]
    raise ValueError(f"unknown column type {type_name!r}")


def _field_for(column: Column) -> Any:
    kwargs: dict[str, Any] = {}
    if column.description:
        kwargs["description"] = column.description
    low, high = _numeric_bounds(column)
    if low is not None:
        kwargs["ge"] = low
    if high is not None:
        kwargs["le"] = high
    return Field(**kwargs)


def _numeric_bounds(column: Column) -> tuple[Any, Any]:
    """A declared numeric range as (ge, le); a string bound containing "inf"
    (the schema's unbounded sentinel) becomes None on that side."""
    if column.range is None or column.type not in ("int", "float"):
        return (None, None)
    low, high = column.range
    if isinstance(low, str):
        low = None
    if isinstance(high, str):
        high = None
    return (low, high)
