"""Named schemas — the data model, authored before the DAG.

A NamedSchema is a TableSchema (the anonymous, inline schema a stage can make up
on the fly) promoted to a first-class, addressable artifact: it adds a `name`, a
`kind` (where it sits in the pipeline), and explicit foreign keys (`references` on
its columns) so the data model is a real graph rather than a PK-name-collision
heuristic. A SchemaLibrary is the whole data model: it checks names are unique and
every reference resolves.

Like methodology.py, the cross-schema checks are plain functions so they can be
tested and read on their own.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import Field, ValidationError, field_validator, model_validator

from app.models.schema import (
    Column,
    SourceRef,
    TableSchema,
    _Base,
    _SNAKE_RE,
    format_errors,
)


class SchemaKind(str, Enum):
    reference = "reference"        # source, don't compute (dimension / lookup / benchmark)
    input = "input"                # raw data fetched into the pipeline
    computed = "computed"          # produced by a DAG stage
    ground_truth = "ground_truth"  # external truth used only by eval


class NamedColumn(Column):
    """A Column that may carry a foreign key (`references`) to another named
    schema, by name or `schema.column`."""
    references: Optional[str] = None


def parse_reference(ref: str) -> tuple[str, Optional[str]]:
    """`"company"` -> (company, None); `"company.company_id"` -> (company, company_id)."""
    if "." in ref:
        schema_name, col = ref.split(".", 1)
        return schema_name.strip(), col.strip()
    return ref.strip(), None


class NamedSchema(TableSchema):
    """One named table in the data model — a TableSchema with a `name`, a `kind`,
    and foreign-key-carrying columns. Column uniqueness and primary-key membership
    are validated by TableSchema."""
    name: str
    kind: SchemaKind
    title: str
    columns: list[NamedColumn] = Field(default_factory=list)
    description: Optional[str] = None
    source: Optional[SourceRef] = None
    # Exclusive arcs: "exactly one of these columns is non-null per row" — the XOR
    # foreign key (e.g. a row scores a company XOR an influencer). Spec-level checks
    # here; the exactly-one-set check on actual rows is runtime data validation.
    exclusive_arcs: Optional[list[list[str]]] = None

    @field_validator("name")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"name {v!r} should be snake_case")
        return v

    @model_validator(mode="after")
    def _arcs_well_formed(self) -> "NamedSchema":
        col_by_name = {c.name: c for c in self.columns}
        for arc in self.exclusive_arcs or []:
            if len(arc) < 2:
                raise ValueError(f"`{self.name}`: each exclusive_arc must list >= 2 columns")
            for cname in arc:
                col = col_by_name.get(cname)
                if col is None:
                    raise ValueError(
                        f"`{self.name}`: exclusive_arc column `{cname}` is not declared")
                if col.nullable is False:
                    raise ValueError(
                        f"`{self.name}`: exclusive_arc column `{cname}` must be nullable "
                        f"(exactly one of the arc is set per row)")
        return self


def check_unique_schema_names(schemas: list[NamedSchema]) -> None:
    names = [s.name for s in schemas]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        raise ValueError(f"duplicate schema name(s): {dupes}")


def check_references_resolve(schemas: list[NamedSchema]) -> None:
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
    """The whole data model: named schemas with unique names and resolvable FKs."""
    schemas: list[NamedSchema]

    @model_validator(mode="after")
    def _validate_library(self) -> "SchemaLibrary":
        check_unique_schema_names(self.schemas)
        check_references_resolve(self.schemas)
        return self


def parse_schema_library(schemas: list[dict[str, Any]]) -> SchemaLibrary:
    """Parse + validate the data model. Raises ValidationError if invalid."""
    return SchemaLibrary.model_validate({"schemas": list(schemas)})


def validate_schema_library(schemas: list[dict[str, Any]]) -> list[str]:
    """Non-fatal: validate the data model, return issues ([] means valid)."""
    try:
        SchemaLibrary.model_validate({"schemas": list(schemas)})
        return []
    except ValidationError as err:
        return format_errors(err)
