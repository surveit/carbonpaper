"""submit_answer never advertises exactly one argument.

A tool whose whole parameter list is one array-of-objects gets called as
{"prop": {"prop": [...]}} — measured at 12/12 runs, 0/9 with any second argument.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.agent.agent import (
    COMPANION_FIELD,
    Agent,
    advertise_more_than_one_argument,
)


class Row(BaseModel):
    name: str


class OneField(BaseModel):
    tests: list[Row]


class TwoFields(BaseModel):
    tests: list[Row]
    note: str


def _advertised(model: type[BaseModel]) -> dict:
    return advertise_more_than_one_argument(model.model_json_schema())


def test_a_one_field_answer_is_advertised_with_a_companion_argument():
    schema = _advertised(OneField)
    assert set(schema["properties"]) == {"tests", COMPANION_FIELD}
    assert schema["properties"][COMPANION_FIELD]["type"] == "boolean"
    assert COMPANION_FIELD in schema["required"]


def test_an_answer_that_already_has_two_fields_is_left_alone():
    assert _advertised(TwoFields) == TwoFields.model_json_schema()


def test_the_companion_argument_is_discarded_rather_than_validated():
    agent: Agent[OneField] = Agent(
        system_prompt="", target_schema=OneField, task="")
    agent.submit_answer(tests=[{"name": "a"}], **{COMPANION_FIELD: True})
    assert agent.answer == OneField(tests=[Row(name="a")])


def test_a_genuinely_invalid_answer_is_still_rejected():
    agent: Agent[OneField] = Agent(
        system_prompt="", target_schema=OneField, task="")
    with pytest.raises(ValueError, match="Submission rejected"):
        agent.submit_answer(tests="not a list", **{COMPANION_FIELD: True})
    assert agent.answer is None


def test_every_agent_tool_advertises_more_than_one_argument():
    from app.compiler.data_model import SchemaLibrary
    from app.models.review_guide import ReviewGuideDraft as ReviewGuide

    for model in (SchemaLibrary, ReviewGuide, OneField, TwoFields):
        assert len(_advertised(model)["properties"]) > 1, model.__name__
