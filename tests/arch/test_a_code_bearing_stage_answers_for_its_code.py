"""Architecture: a stage type holding code answers find_authored_code_block.
docs/branch-analysis.md"""
from __future__ import annotations

from typing import get_args

from pydantic import BaseModel

from app.models.stage import Stage
from app.models.stages.stage_base import AbstractStage


def find_code_bearing_members() -> list[type[AbstractStage]]:
    return [member for member in get_args(get_args(Stage)[0]) if _holds_code(member)]


def find_members_that_hide_their_code() -> list[str]:
    return sorted(member.__name__ for member in find_code_bearing_members()
                  if member.find_authored_code_block is AbstractStage.find_authored_code_block)


def _holds_code(member: type[AbstractStage]) -> bool:
    return any(_declares_code(field.annotation) for field in member.model_fields.values())


def _declares_code(annotation: object) -> bool:
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "code" in annotation.model_fields
    return any(_declares_code(argument) for argument in get_args(annotation))


def test_every_stage_type_holding_code_declares_the_block_it_holds() -> None:
    assert find_code_bearing_members(), "the union declared no code-bearing stage type"
    assert find_members_that_hide_their_code() == []
