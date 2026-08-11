"""Rendering of run status onto the workflow mermaid graph."""
from __future__ import annotations

from app.models import parse_stage
from app.web.diagrams import build_mermaid_graph


def test_cancelled_stage_gets_glyph_and_grey_stroke() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", status_by_id={"s1": "cancelled"})
    assert "✖" in graph
    assert "stroke:#787d86" in graph


def test_running_stage_gets_glyph_and_a_dashed_grey_stroke() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", status_by_id={"s1": "running"})
    assert "⟳" in graph
    assert "stroke:#787d86,stroke-width:3px,stroke-dasharray:6 4" in graph


def test_a_stage_that_reached_a_verdict_takes_no_dashes() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", status_by_id={"s1": "ok"})
    assert "stroke-dasharray" not in graph


def test_plain_stage_with_no_status_renders_the_bare_node() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo")
    assert graph.startswith("flowchart LR")
    assert '    s1["<b>⬆️ s1</b><br/><span' in graph   # the id is the node's one name
    assert 'click s1 call dvNode("s1") "Stage One"' in graph   # description is the tooltip
    assert "]:::input" in graph
    assert "stroke:" not in graph.split("classDef")[0]
    assert "    classDef input fill:#fdfdfe,stroke:#e9e9eb,color:#24272b" in graph


def test_every_node_class_gets_the_same_neutral_surface() -> None:
    surfaces = {
        line.strip().split(" ", 2)[2]
        for line in build_mermaid_graph([], "demo").splitlines()
        if line.strip().startswith("classDef ")
    }
    assert len(surfaces) == 1, f"stage types are still fill-coded: {sorted(surfaces)}"


def test_notes_eval_and_review_indicators_all_appear() -> None:
    stages = [{
        "id": "s1", "description": "Stage One", "type": "aggregate",
        "compiler_notes": "watch this", "eval": {"metrics": ["recall"]},
        "review": {"belief": "approved"},
    }]
    graph = build_mermaid_graph(stages, "demo")
    assert "⚠ " in graph          # has_notes
    assert "📊" in graph           # has_eval flag glyph (also the aggregate type glyph)
    assert "👤" in graph           # has_review flag glyph


def test_an_unrecognized_status_draws_no_stroke_override() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "input_data"}]
    graph = build_mermaid_graph(stages, "demo", status_by_id={"s1": "some_unmapped_status"})
    assert "style s1 stroke:" not in graph


def test_unknown_stage_type_gets_the_custom_class_and_no_glyph() -> None:
    stages = [{"id": "s1", "description": "Stage One", "type": "mystery"}]
    graph = build_mermaid_graph(stages, "demo")
    assert "]:::custom" in graph
    assert '"<b> s1</b>' in graph   # no glyph prefix (glyph slot left as a bare space)


def test_edges_are_drawn_from_input_ids() -> None:
    stages: list[dict[str, object]] = [
        {"id": "a", "description": "A", "type": "input_data"},
        {"id": "b", "description": "B", "type": "aggregate", "inputs": ["a"]},
    ]
    graph = build_mermaid_graph(stages, "demo")
    assert "    a --> b" in graph


def test_typed_stage_input_renders_the_same_as_the_equivalent_draft_dict(tmp_path) -> None:
    stage = parse_stage({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file",
                      "params": {"path": str(tmp_path / "d.csv"), "format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [{"name": "k", "type": "str", "nullable": True}],
        },
    })
    typed_graph = build_mermaid_graph([stage], "demo")
    dict_graph = build_mermaid_graph(
        [{"id": "load", "description": "Load", "type": "input_data"}], "demo"
    )
    assert typed_graph == dict_graph
