"""Tests for the llm_transform stage's config-column check (app/models/
stages/llm_transform.py, wired into Stage._config_columns_resolve): every
`{placeholder}` the prompt template actually interpolates must resolve
against the stage's own input edge schema.

(There is no "edge declares no schema" case here to test: Stage's own
_llm_transform_one_to_one validator already requires a primary_key on both
the input and output schema, so a validly-constructed llm_transform's input
edge always declares a schema.)"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import Stage


def _llm_stage(prompt_template):
    return {
        "id": "ask", "type": "llm_transform", "name": "ask",
        "inputs": [{"id": "src", "schema": {
            "columns": [{"name": "a", "type": "str", "nullable": False}],
            "primary_key": ["a"],
        }}],
        "output_schema": {
            "columns": [{"name": "a", "type": "str", "nullable": False},
                        {"name": "verdict", "type": "str", "nullable": False}],
            "primary_key": ["a"],
        },
        "llm": {"prompt_template": prompt_template},
    }


def test_prompt_field_missing_from_edge_schema_rejected():
    with pytest.raises(ValidationError):
        Stage.model_validate(_llm_stage("judge {a} and also {ghost}"))


def test_prompt_field_present_ok():
    Stage.model_validate(_llm_stage("judge {a}"))


def test_prompt_with_no_fields_is_clean():
    Stage.model_validate(_llm_stage("judge the row"))
