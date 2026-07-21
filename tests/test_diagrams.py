"""Rendering of run status onto the workflow mermaid graph."""
from __future__ import annotations

from app.web.diagrams import build_mermaid_graph


def test_cancelled_stage_gets_glyph_and_grey_stroke() -> None:
    """A stage whose run status is 'cancelled' (set when a run is cancelled
    during that stage's fan-out) gets the ✖ glyph in its node label and a grey
    stroke override — the same distinct treatment other terminal statuses get,
    so the cancelled stage is visible in the graph rather than rendering as an
    unstyled default node."""
    stages = [{"id": "s1", "name": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", status_by_id={"s1": "cancelled"})
    assert "✖" in graph
    assert "stroke:#8a8a8a" in graph
