"""A RowType is the methodology's word for what ONE ROW is."""
from __future__ import annotations

from pydantic import ConfigDict, field_validator

from app.core.ids import ID
from app.models.schema import _Base, _SNAKE_RE
from app.models.tool_schema_prompts import ROW_TYPE_DESCRIPTION

# What a stage writes where its output rows are not a kind of thing; no project may hold it.
NO_KIND_ROW_TYPE_ID = "no_kind"


class RowType(_Base):
    model_config = ConfigDict(json_schema_extra={"description": ROW_TYPE_DESCRIPTION})

    id: ID
    title: str
    definition: str

    @field_validator("id")
    @classmethod
    def _snake_case(cls, v: str) -> str:
        if not _SNAKE_RE.match(v):
            raise ValueError(f"id {v!r} should be snake_case")
        return v

    @field_validator("id")
    @classmethod
    def _no_row_type_is_the_reserved_word(cls, v: str) -> str:
        if v == NO_KIND_ROW_TYPE_ID:
            raise ValueError(
                f"id {v!r} is reserved for the stages whose output rows are not a kind of "
                f"thing, so no row type may be declared under it"
            )
        return v
