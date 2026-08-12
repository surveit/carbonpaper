"""A project's terms: the nouns its methodology runs on (the data model) and its verbs.
A noun with no columns and no kind is vocabulary alone — a word the methodology uses.
"""
from __future__ import annotations

from pydantic import ConfigDict, Field, TypeAdapter, model_validator

from app.models.named_schemas import SchemaLibrary
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
    words = [schema.name for schema in nouns.schemas]
    for verb in verbs:
        words += [verb.name, *verb.also_written]
    repeated = sorted({word for word in words if words.count(word) > 1})
    if repeated:
        raise ValueError(f"word(s) carrying more than one meaning: {repeated}")
