"""Every template that renders a stage graph must subscribe to node clicks.

mermaid resolves a node's click callback by NAME at click time, so a page that
never wires a handler gets nodes that look live and silently do nothing — how
version_detail.html shipped broken. See app/static/diagram_nodes.js.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"


def _templates_rendering_a_stage_graph() -> list[Path]:
    found = []
    for path in sorted(TEMPLATES.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if 'class="mermaid"' in text and "{{ mermaid }}" in text:
            found.append(path)
    return found


def test_every_stage_graph_template_subscribes_to_node_clicks() -> None:
    found = _templates_rendering_a_stage_graph()
    assert found, "no stage-graph templates found — has the graph markup moved?"
    unwired = [
        p.name for p in found if "onDiagramNode(" not in p.read_text(encoding="utf-8")
    ]
    assert not unwired, (
        f"{unwired} render a clickable stage graph but never call onDiagramNode(). "
        "Their nodes would look clickable and silently do nothing."
    )


def test_no_template_defines_its_own_mermaid_click_global() -> None:
    offenders = [
        p.name for p in TEMPLATES.glob("*.html")
        if "window.loadStage" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        f"{offenders} define window.loadStage. Subscribe with onDiagramNode() instead — "
        "see static/diagram_nodes.js."
    )
