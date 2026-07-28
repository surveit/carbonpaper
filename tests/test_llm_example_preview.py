"""build_llm_example: the first-row prompt preview shown on the loading page
for an llm_transform stage — rendered from prompt_data_template (the
per-row part), not the row-invariant prompt_instructions."""
from __future__ import annotations

from app.models import Stage
from app.web.loading import build_llm_example


def _llm_stage() -> Stage:
    return Stage.model_validate({
        "id": "score", "type": "llm_transform", "name": "Score",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"}],
            "primary_key": ["id"]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str"}, {"name": "quote", "type": "str"},
                        {"name": "score", "type": "int", "nullable": False}],
            "primary_key": ["id"]},
        "llm": {"prompt_instructions": "Score for relevance.",
                "prompt_data_template": "Rate this: {quote}"},
    })


def test_renders_from_prompt_data_template_not_instructions():
    previews = [{"id": "load", "preview": {"preview": [{"quote": "Quote about widgets."}]}}]
    example = build_llm_example(_llm_stage(), previews)
    assert example == {"source_id": "load", "rendered": "Rate this: Quote about widgets."}


def test_no_input_rows_reports_error():
    example = build_llm_example(_llm_stage(), [])
    assert example == {"error": "no input rows available in this run to render an example"}


def test_non_llm_stage_returns_none():
    stage = Stage.model_validate({
        "id": "load", "name": "Load", "type": "input_data",
        "output_schema": {"columns": [{"name": "quote", "type": "str"}]},
        "connector": {"kind": "file"},
    })
    assert build_llm_example(stage, [{"id": "load", "preview": {"preview": [{"quote": "x"}]}}]) is None
