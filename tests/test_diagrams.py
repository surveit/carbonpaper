"""Rendering of run status onto the workflow mermaid graph."""
from __future__ import annotations

from app.models import Stage
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


def test_plain_stage_with_no_status_or_review_renders_the_bare_node() -> None:
    """No status_by_id and no review_by_id: no status prefix, no stroke
    override line at all, and the type-class/glyph/classDef scaffold is the
    same regardless."""
    stages = [{"id": "s1", "name": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo")
    assert graph.startswith("flowchart LR")
    assert '    s1["<b>⬆️ Stage One</b><br/><span' in graph
    assert 'click s1 call loadStage("s1") "Open stage"' in graph
    assert "]:::input" in graph
    assert "stroke:" not in graph.split("classDef")[0]
    assert "    classDef custom fill:#fde8e8,stroke:#cc3333,color:#000" in graph


def test_notes_eval_and_review_indicators_all_appear() -> None:
    stages = [{
        "id": "s1", "name": "Stage One", "type": "aggregate",
        "compiler_notes": "watch this", "eval": {"metrics": ["recall"]},
        "review": {"belief": "approved"},
    }]
    graph = build_mermaid_graph(stages, "demo")
    assert "⚠ " in graph          # has_notes
    assert "📊" in graph           # has_eval flag glyph (also the aggregate type glyph)
    assert "👤" in graph           # has_review flag glyph


def test_review_belief_colours_the_stroke_when_no_status_given() -> None:
    stages = [{"id": "s1", "name": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", review_by_id={"s1": "rejected"})
    assert "stroke:#cc2a2a,stroke-width:3px" in graph


def test_run_status_stroke_wins_over_review_belief_when_both_given() -> None:
    stages = [{"id": "s1", "name": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(
        stages, "demo",
        status_by_id={"s1": "error"}, review_by_id={"s1": "approved"},
    )
    assert "stroke:#cc2a2a,stroke-width:3px" in graph   # ERROR red, not approved green
    assert "#2a8a2a" not in graph


def test_unrecognized_status_falls_back_to_review_belief_stroke() -> None:
    """A status string outside status_stroke's keys does not itself draw a
    stroke, but does not suppress the belief fallback either — the `else`
    branch runs whenever `status` isn't a stroke-carrying status, whether or
    not it was given at all."""
    stages = [{"id": "s1", "name": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(
        stages, "demo",
        status_by_id={"s1": "some_unmapped_status"}, review_by_id={"s1": "approved"},
    )
    assert "stroke:#2a8a2a,stroke-width:3px" in graph


def test_unknown_stage_type_gets_the_custom_class_and_no_glyph() -> None:
    stages = [{"id": "s1", "name": "Stage One", "type": "mystery"}]
    graph = build_mermaid_graph(stages, "demo")
    assert "]:::custom" in graph
    assert '"<b> Stage One</b>' in graph   # no glyph prefix (glyph slot left as a bare space)


def test_edges_are_drawn_from_input_ids() -> None:
    stages: list[dict[str, object]] = [
        {"id": "a", "name": "A", "type": "input_data"},
        {"id": "b", "name": "B", "type": "aggregate", "inputs": ["a"]},
    ]
    graph = build_mermaid_graph(stages, "demo")
    assert "    a --> b" in graph


def test_typed_stage_input_renders_the_same_as_the_equivalent_draft_dict(tmp_path) -> None:
    """build_mermaid_graph also accepts real Stage objects (the isinstance(s,
    Stage) branch of _node_view) — pinned so the two input shapes stay
    interchangeable."""
    stage = Stage.model_validate({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
    })
    typed_graph = build_mermaid_graph([stage], "demo")
    dict_graph = build_mermaid_graph(
        [{"id": "load", "name": "Load", "type": "input_data"}], "demo"
    )
    assert typed_graph == dict_graph
