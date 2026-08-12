"""A project's terms: the nouns its methodology runs on (the data model) and its verbs.
A noun with no columns and no kind is vocabulary alone — a word the methodology uses.
"""
from __future__ import annotations

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from app.models.named_schemas import NamedSchema, SchemaLibrary
from app.models.schema import _Base
from app.models.tool_schema_prompts import (
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

    nouns: SchemaLibrary
    verbs: list[Verb] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_terms(self) -> "Terms":
        validate_one_meaning_per_word(self.nouns, self.verbs)
        return self


_VERB_LIST: TypeAdapter[list[Verb]] = TypeAdapter(list[Verb])


def parse_verbs(payload: str) -> list[Verb]:
    return _VERB_LIST.validate_json(payload)


def validate_one_meaning_per_word(nouns: SchemaLibrary, verbs: list[Verb]) -> None:
    words: list[str] = []
    for schema in nouns.schemas:
        words += [schema.name, *schema.also_written]
    for verb in verbs:
        words += [verb.name, *verb.also_written]
    repeated = sorted({word for word in words if words.count(word) > 1})
    if repeated:
        raise ValueError(f"word(s) carrying more than one meaning: {repeated}")


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
        _render_word_list("Nouns:", [_render_noun(noun) for noun in terms.nouns.schemas]),
        _render_word_list("Verbs:", [_render_verb(verb) for verb in terms.verbs]),
    ]
    written = [block for block in blocks if block]
    if not written:
        return ""
    return "\n\n".join([_TERMS_FRAMING, *written])


def _render_word_list(heading: str, words: list[str]) -> str:
    return "\n".join([heading, *words]) if words else ""


def _render_noun(noun: NamedSchema) -> str:
    # A noun that is vocabulary and nothing more carries no description, only its title.
    return _render_word(noun.name, noun.description or noun.title, noun.also_written)


def _render_verb(verb: Verb) -> str:
    return _render_word(verb.name, verb.definition, verb.also_written)


def _render_word(name: str, definition: str, also_written: list[str]) -> str:
    spellings = f" Also written: {', '.join(also_written)}." if also_written else ""
    return f"- {name} — {definition}{spellings}"
