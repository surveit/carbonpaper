"""build_llm_example: the first-row prompt preview shown on the loading page
for an llm_transform stage — rendered from prompt_data_template (the
per-row part), not the row-invariant prompt_instructions."""
from __future__ import annotations

from app.models import WorkflowStage, parse_stage
from app.web.loading import build_llm_example
from conftest import place_stage


def _llm_stage() -> WorkflowStage:
    return place_stage(parse_stage({
        "id": "score", "type": "llm_transform", "description": "Score",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "quote", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "score", "type": "int", "nullable": False}],
        },
        "llm": {"prompt_instructions": "Score for relevance.",
                "prompt_data_template": "Rate this: {quote}"},
    }))


def test_renders_from_prompt_data_template_not_instructions():
    previews = [{"id": "load", "preview": {"preview": [{"quote": "Quote about widgets."}]}}]
    example = build_llm_example(_llm_stage(), previews)
    assert example == {"source_id": "load", "rendered": "Rate this: Quote about widgets."}


def test_no_input_rows_reports_error():
    example = build_llm_example(_llm_stage(), [])
    assert example == {"error": "no input rows available in this run to render an example"}


def test_non_llm_stage_returns_none():
    stage = place_stage(parse_stage({
        "id": "load", "description": "Load", "type": "input_data",
        "signature": {
            "form": "replaces",
            "produces": [{"name": "quote", "type": "str", "nullable": True}],
        },
        "connector": {"kind": "file"},
    }))
    assert build_llm_example(stage, [{"id": "load", "preview": {"preview": [{"quote": "x"}]}}]) is None
