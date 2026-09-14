"""A project's terms: its words — row types and verbs — and the tables of its data model."""
from __future__ import annotations

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from app.models.named_schemas import SchemaLibrary
from app.models.row_types import RowType
from app.models.schema import _Base
from app.models.tool_schema_prompts import (
    ROW_TYPES_AND_SCHEMAS_DESCRIPTION,
    TERMS_DESCRIPTION,
    VERB_ALSO_WRITTEN_DESCRIPTION,
    VERB_DESCRIPTION,
)


class Verb(_Base):
    model_config = ConfigDict(json_schema_extra={"description": VERB_DESCRIPTION})

    name: str
    definition: str
    also_written: list[str] = Field(
        default_factory=list, description=VERB_ALSO_WRITTEN_DESCRIPTION
    )


class Terms(_Base):
    model_config = ConfigDict(json_schema_extra={"description": TERMS_DESCRIPTION})

    row_types: list[RowType] = Field(default_factory=list)
    schemas: SchemaLibrary = Field(
        default_factory=lambda: SchemaLibrary(schemas=[])
    )
    verbs: list[Verb] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_terms(self) -> "Terms":
        validate_one_meaning_per_word(self.row_types, self.verbs)
        validate_row_type_ids_resolve(self.row_types, self.schemas)
        return self


# Terms minus the verbs: the half a generator authors, where a human agrees the rest.
class RowTypesAndSchemas(_Base):
    model_config = ConfigDict(
        json_schema_extra={"description": ROW_TYPES_AND_SCHEMAS_DESCRIPTION}
    )

    row_types: list[RowType]
    schemas: SchemaLibrary

    @model_validator(mode="after")
    def _validate_row_type_ids(self) -> "RowTypesAndSchemas":
        validate_row_type_ids_resolve(self.row_types, self.schemas)
        return self


_VERB_LIST: TypeAdapter[list[Verb]] = TypeAdapter(list[Verb])


def parse_verbs(payload: str) -> list[Verb]:
    return _VERB_LIST.validate_json(payload)


def validate_one_meaning_per_word(row_types: list[RowType], verbs: list[Verb]) -> None:
    # A schema name addresses a table rather than saying a word, so it is not in here.
    words: list[str] = []
    for row_type in row_types:
        words += [row_type.id, *row_type.also_written]
    for verb in verbs:
        words += [verb.name, *verb.also_written]
    repeated = sorted({word for word in words if words.count(word) > 1})
    if repeated:
        raise ValueError(f"word(s) carrying more than one meaning: {repeated}")


def validate_row_type_ids_resolve(row_types: list[RowType], schemas: SchemaLibrary) -> None:
    declared = {row_type.id for row_type in row_types}
    for schema in schemas.schemas:
        if schema.row_type_id is not None and schema.row_type_id not in declared:
            raise ValueError(
                f"`{schema.name}`: row_type_id `{schema.row_type_id}` names no declared row type")


# ─── The block every agent writing about a project is handed ─────────────────
# Here rather than app.tools.prompt_fragments, where the rest of the prompt copy
# sits: app.compiler renders this too, and the import-linter admits only
# app.agents and app.mcp into app.tools.

_TERMS_FRAMING = """\
# Terms
The methodology owner's own words for this project. Write in them — a synonym you
prefer for one of them is a second name for the same thing, and is not introduced."""


def render_terms(terms: Terms) -> str:
    """Nothing at all for a project with no words: a heading over none teaches the wrong lesson."""
    blocks = [
        _render_word_list("Row types:", [_render_row_type(rt) for rt in terms.row_types]),
        _render_word_list("Verbs:", [_render_verb(verb) for verb in terms.verbs]),
    ]
    written = [block for block in blocks if block]
    if not written:
        return ""
    return "\n\n".join([_TERMS_FRAMING, *written])


def _render_word_list(heading: str, words: list[str]) -> str:
    return "\n".join([heading, *words]) if words else ""


def _render_row_type(row_type: RowType) -> str:
    return _render_word(row_type.id, row_type.definition, row_type.also_written)


def _render_verb(verb: Verb) -> str:
    return _render_word(verb.name, verb.definition, verb.also_written)


def _render_word(name: str, definition: str, also_written: list[str]) -> str:
    spellings = f" Also written: {', '.join(also_written)}." if also_written else ""
    return f"- {name} — {definition}{spellings}"
