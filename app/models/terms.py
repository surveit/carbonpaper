"""A project's terms: its words — row types and verbs — and the tables of its data model."""
from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from app.models.named_schemas import SchemaLibrary
from app.models.row_types import RowType
from app.models.schema import _Base
from app.models.tool_schema_prompts import (
    TERMS_DESCRIPTION,
    VERB_DESCRIPTION,
)


class Verb(_Base):
    model_config = ConfigDict(json_schema_extra={"description": VERB_DESCRIPTION})

    name: str
    definition: str


class Terms(_Base):
    model_config = ConfigDict(json_schema_extra={"description": TERMS_DESCRIPTION})

    row_types: list[RowType] = Field(default_factory=list)
    schemas: SchemaLibrary = Field(
        default_factory=lambda: SchemaLibrary(schemas=[])
    )
    verbs: list[Verb] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_terms(self) -> "Terms":
        validate_no_word_is_written_twice(self.row_types, self.verbs)
        return self


def validate_no_word_is_written_twice(row_types: list[RowType], verbs: list[Verb]) -> None:
    # A schema name addresses a table rather than saying a word, so it is not in here.
    words = [row_type.id for row_type in row_types] + [verb.name for verb in verbs]
    repeated = sorted({word for word in words if words.count(word) > 1})
    if repeated:
        raise ValueError(f"word(s) written twice: {repeated}")


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
    return f"- {row_type.id} — {row_type.definition}"


def _render_verb(verb: Verb) -> str:
    return f"- {verb.name} — {verb.definition}"
