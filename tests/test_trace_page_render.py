import pytest
from jinja2 import UndefinedError

from app.models import Stage
from app.runtime.trace_page import render_trace_body
from app.runtime.trace_view import build_trace_view


def _stages() -> dict[str, Stage]:
    return {
        "load": Stage.model_validate({
            "id": "load", "type": "input_data", "name": "Load",
            "connector": {"kind": "file"},
            "output_schema": {"columns": [{"name": "name", "type": "str"}],
                              "primary_key": ["name"]},
        }),
        "score": Stage.model_validate({
            "id": "score", "type": "llm_transform", "name": "Score",
            "inputs": [{"id": "load", "schema": {
                "columns": [{"name": "name", "type": "str"}],
                "primary_key": ["name"]}}],
            "output_schema": {
                "columns": [{"name": "name", "type": "str"},
                            {"name": "risk", "type": "str", "nullable": False}],
                "primary_key": ["name"]},
            "llm": {"prompt_instructions": "Rate the risk.",
                     "prompt_data_template": "Name: {name}"},
        }),
    }


def _trace() -> dict:
    # trace_to_dict shape: steps newest-first, end at the origin.
    return {
        "run_id": "R1", "start_stage": "score", "start_row": 0,
        "steps": [
            {"stage_id": "score", "stage_type": "llm_transform", "row_ordinal": 0,
             "row": {"name": "Acme", "risk": "high"}, "columns_new": ["risk"], "origin": "computed"},
            {"stage_id": "load", "stage_type": "input_data", "row_ordinal": 0,
             "row": {"name": "Acme"}, "columns_new": ["name"], "origin": "source"},
        ],
        "end": {"reached_origin": True, "at_stage": "load",
                "message": "input_data stage — the rows originate here"},
    }


def _view() -> dict:
    return build_trace_view(_trace(), _stages())


def test_body_shows_every_stage_in_the_chain():
    html = render_trace_body(_view())
    assert "load" in html and "score" in html


def test_body_shows_the_values_the_row_carried():
    html = render_trace_body(_view())
    assert "Acme" in html and "high" in html


def test_body_is_a_fragment_not_a_document():
    html = render_trace_body(_view())
    assert "<html" not in html.lower()
    assert "<body" not in html.lower()


def test_missing_required_field_raises_rather_than_rendering_empty():
    with pytest.raises(UndefinedError):
        render_trace_body({})
