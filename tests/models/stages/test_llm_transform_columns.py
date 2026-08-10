"""No "edge declares no schema" case exists to test: `schema` is a required field
on StageInput, so a validly-constructed llm_transform's input edge always
declares one."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.prompt_template import find_template_fields
from app.models import parse_stage


_EDGE_COLUMNS = [{"name": "a", "type": "str", "nullable": False}]


def _llm_stage(prompt_template):
    injected = find_template_fields(prompt_template)
    return {
        "id": "ask", "type": "llm_transform", "description": "ask",
        "inputs": [{"id": "src", "schema": {"columns": _EDGE_COLUMNS}}],
        "signature": {
            "form": "extends",
            # An InputReads entry needs at least one column, so a template that
            # injects nothing reads nothing at all.
            "reads": [{"input": "src", "columns": read}] if (
                read := [c for c in _EDGE_COLUMNS if c["name"] in injected]) else [],
            "adds": [{"name": "verdict", "type": "str", "nullable": False}],
        },
        "llm": {"prompt_template": prompt_template},
    }


def test_prompt_field_missing_from_edge_schema_rejected():
    with pytest.raises(ValidationError):
        parse_stage(_llm_stage("judge {a} and also {ghost}"))


def test_prompt_field_present_ok():
    parse_stage(_llm_stage("judge {a}"))


def test_prompt_with_no_fields_is_clean():
    parse_stage(_llm_stage("judge the row"))
