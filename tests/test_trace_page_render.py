from app.runtime.trace_page import render_trace_body

VIEW = {
    "run_id": "20260101T000000",
    "stage_id": "score",
    "row_ordinal": 3,
    "nodes": [
        {"stage_id": "load", "kind": "source", "new_columns": ["name"],
         "row": {"name": "Acme"}, "transform": {"kind": "source", "detail": ""}},
        {"stage_id": "score", "kind": "llm", "new_columns": ["risk"],
         "row": {"name": "Acme", "risk": "high"},
         "transform": {"kind": "llm", "detail": "rate the risk"}},
    ],
    "end": {"complete": True, "stage_id": "load", "message": ""},
}


def test_body_shows_every_stage_in_the_chain():
    html = render_trace_body(VIEW)
    assert "load" in html and "score" in html


def test_body_shows_the_values_the_row_carried():
    html = render_trace_body(VIEW)
    assert "Acme" in html and "high" in html


def test_body_is_a_fragment_not_a_document():
    html = render_trace_body(VIEW)
    assert "<html" not in html.lower()
    assert "<body" not in html.lower()
