"""build_llm_example: the first-row prompt preview shown on the loading page
for an llm_transform stage — rendered from prompt_data_template (the
per-row part), not the row-invariant prompt_instructions."""
from __future__ import annotations

from app.models import parse_stage, Stage
from app.web.loading import build_llm_example


def _llm_stage() -> Stage:
    return parse_stage({
        "id": "score", "type": "llm_transform", "name": "Score",
        "inputs": [{"id": "load", "schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True}]}}],
        "output_schema": {
            "columns": [{"name": "id", "type": "str", "nullable": True}, {"name": "quote", "type": "str", "nullable": True},
                        {"name": "score", "type": "int", "nullable": False}]},
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
    stage = parse_stage({
        "id": "load", "name": "Load", "type": "input_data",
        "output_schema": {"columns": [{"name": "quote", "type": "str", "nullable": True}]},
        "connector": {"kind": "file"},
    })
    assert build_llm_example(stage, [{"id": "load", "preview": {"preview": [{"quote": "x"}]}}]) is None
