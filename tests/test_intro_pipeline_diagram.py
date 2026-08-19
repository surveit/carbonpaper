from pathlib import Path


INTRO_PAGE = Path("intro/index.html")


def test_pipeline_centers_single_line_labels_and_uses_visible_neutral_arrows() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert page.count('class="gname"') == 8
    assert page.count('y="121" text-anchor="middle"') == 6
    assert 'x="926" y="77" text-anchor="middle">Outside firms' in page
    assert 'x="926" y="167" text-anchor="middle">In-house lobbying' in page
    assert '.gflow  { stroke: #9aa1ab; stroke-width: 1.3; fill: none; }' in page
    assert '<marker id="ar"' in page
    assert '<marker id="ar-lit"' in page


def test_pipeline_neutralizes_unfocused_node_borders() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert '.gmodel { fill: #FFFFFF; stroke: #787d86; stroke-width: 2; }' in page
    assert '.ghuman { fill: #FFFFFF; stroke: #787d86; stroke-width: 2; }' in page
    assert '.gchoice{ fill: #FFFFFF; stroke: #787d86; stroke-width: 2; }' in page
    assert '#pipe .node.lit rect { stroke: #1d539c; }' in page
    assert '#pipe.focus .node:not(.lit) rect { stroke: #787d86; }' in page
    assert 'markerUnits="userSpaceOnUse"' in page


def test_export_overlay_is_centered_and_above_the_pipeline() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert "11: {ask: 1, layer: 'graph', out: 'ran', recede: 1, over: '11'}" in page
    assert "pipe.classList.toggle('recede', !!scene.recede);" in page
    assert '#pipe.recede { opacity: .26; }' in page
    assert '.over.on { opacity: 1; visibility: visible; z-index: 3; }' in page
    assert 'data-over="11" style="left: 50%; top: 50%; transform: translate(-50%, -50%);"' in page


def test_highlighted_arrowheads_use_fixed_size_blue_marker() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert page.count('d="M0 0.5 L7.5 4 L0 7.5 z"') == 2
    assert '<path d="M0 0.5 L7.5 4 L0 7.5 z" fill="#1d539c"/>' in page
    assert "setAttribute('marker-end', lit ? 'url(#ar-lit)' : 'url(#ar)')" in page


def test_nojs_export_overlay_ignores_centering_position() -> None:
    page = INTRO_PAGE.read_text(encoding="utf-8")

    assert '.nojs .over { left: auto !important; top: auto !important; transform: none !important; }' in page
