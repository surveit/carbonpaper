from __future__ import annotations

from typing import Literal

from pydantic import Field

from critic.records import CriticRecord, JsonSchemaDocument

Severity = Literal["blocking", "should", "nit"]


class PredictedComment(CriticRecord):
    path: str
    line: int = Field(ge=1)
    theme: str
    rule: str
    text: str
    severity: Severity


class ReviewAnswer(CriticRecord):
    comments: list[PredictedComment]


def build_answer_schema(theme_slugs: list[str] | None) -> JsonSchemaDocument:
    schema = ReviewAnswer.model_json_schema()
    if theme_slugs is not None:
        schema["$defs"]["PredictedComment"]["properties"]["theme"]["enum"] = theme_slugs
    return schema
