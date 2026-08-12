"""Named schemas — the data model, authored before the workflow.
A NamedSchema is a TableSchema promoted to an addressable artifact: it adds a `name`, a
`kind` where the data has a source to claim, and explicit foreign keys (`references` on
its columns). A SchemaLibrary checks names are unique and every reference resolves.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import ConfigDict, Field, ValidationError, field_validator, model_validator

from app.models.schema import (
    Column,
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
)
from app.core.utils import format_errors
from app.models.tool_schema_prompts import (
    NAMED_COLUMN_DESCRIPTION,
    NAMED_SCHEMA_DESCRIPTION,
    SCHEMA_LIBRARY_DESCRIPTION,
)


class SchemaKind(str, Enum):
    reference = "reference"        # source, don't compute (dimension / lookup / benchmark)
    input = "input"                # raw data fetched into the pipeline
    computed = "computed"          # produced by a workflow stage
    ground_truth = "ground_truth"  # external truth used only by eval


class NamedColumn(Column):
    model_config = ConfigDict(json_schema_extra={"description": NAMED_COLUMN_DESCRIPTION})

    references: Optional[str] = None


def parse_reference(ref: str) -> tuple[str, Optional[str]]:
    if "." in ref:
        schema_name, col = ref.split(".", 1)
        return schema_name.strip(), col.strip()
    return ref.strip(), None


class NamedSchema(TableSchema):
    model_config = ConfigDict(json_schema_extra={"description": NAMED_SCHEMA_DESCRIPTION})

    name: str
    # Absent where the name is vocabulary and nothing more: a kind claims where the
    # rows come from, and a word with no table behind it makes no such claim.
    kind: Optional[SchemaKind] = None
    title: str
    columns: list[NamedColumn] = Field(default_factory=list)
    # The data model documents source identity for the journalist; the stage
    # vocabulary carries no key (row identity there is a content hash).
    primary_key: Optional[list[str]] = None
    description: Optional[str] = None
    source: Optional[SourceRef] = None

    @model_validator(mode="after")
    def _key_names_declared_columns(self) -> "NamedSchema":
        declared = {c.name for c in self.columns}
        for k in self.primary_key or []:
            if k not in declared:
                raise ValueError(f"primary_key {k!r} is not a declared column")
        return self

    @field_validator("name")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"name {v!r} should be snake_case")
        return v


def validate_unique_schema_names(schemas: list[NamedSchema]) -> None:
    names = [s.name for s in schemas]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate schema name(s): {dupes}")


def validate_references_resolve(schemas: list[NamedSchema]) -> None:
    by_name = {s.name: s for s in schemas}
    for s in schemas:
        for col in s.columns:
            if not col.references:
                continue
            target_name, target_col = parse_reference(col.references)
            target = by_name.get(target_name)
            if target is None:
                raise ValueError(
                    f"`{s.name}`.{col.name}: references unknown schema `{target_name}`")
            if target_col is not None and target_col not in {c.name for c in target.columns}:
                raise ValueError(
                    f"`{s.name}`.{col.name}: references `{target_name}.{target_col}` "
                    f"which is not a column of `{target_name}`")


class SchemaLibrary(_Base):
    model_config = ConfigDict(json_schema_extra={"description": SCHEMA_LIBRARY_DESCRIPTION})

    schemas: list[NamedSchema]

    @model_validator(mode="after")
    def _validate_library(self) -> "SchemaLibrary":
        validate_unique_schema_names(self.schemas)
        validate_references_resolve(self.schemas)
        return self


def validate_named_schema(schema: dict[str, Any]) -> list[str]:
    """Does NOT check that `references` resolve — that is validate_schema_library's, not this."""
    try:
        NamedSchema.model_validate(schema)
        return []
    except ValidationError as err:
        return format_errors(err)


def parse_schema_library(schemas: list[dict[str, Any]]) -> SchemaLibrary:
    return SchemaLibrary.model_validate({"schemas": list(schemas)})


def validate_schema_library(schemas: list[dict[str, Any]]) -> list[str]:
    try:
        SchemaLibrary.model_validate({"schemas": list(schemas)})
        return []
    except ValidationError as err:
        return format_errors(err)
