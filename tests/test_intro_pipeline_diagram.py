from pathlib import Path


INTRO_PAGE = Path("intro/index.html")


def test_pipeline_centers_single_line_labels_and_uses_visible_neutral_arrows() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert page.count('class="gname"') == 8
    assert page.count('y="121" text-anchor="middle"') == 6
    assert 'x="926" y="77" text-anchor="middle">Outside firms' in page
    assert 'x="926" y="167" text-anchor="middle">In-house lobbying' in page
    assert '.gflow  { stroke: #9aa1ab; stroke-width: 1.3; fill: none; }' in page
    assert 'fill="#9aa1ab"' in page


def test_pipeline_neutralizes_unfocused_node_borders() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert '#pipe.focus .node:not(.lit) rect { stroke: #787d86; }' in page
