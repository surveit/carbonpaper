"""Architecture: one favicon, its colours pinned to the palette, linked by every head.

A favicon renders in its own document, so it cannot spend var(--accent) and carries
hexes — the exemption app/web/diagrams.py holds, paid the same way. The second rule
is one this app broke once already, when the lineage page missed palette.css.
"""
from __future__ import annotations

import re
from pathlib import Path
from xml.etree import ElementTree

from arch.test_status_colour_contract import read_declared_colours

_APP = Path(__file__).resolve().parents[2] / "app"
_FAVICON = _APP / "static" / "favicon.svg"
_TEMPLATES = _APP / "templates"

_SVG_COLOUR = re.compile(r'(?:fill|stroke)="(#[0-9a-fA-F]{3,8})"')
_HEAD = re.compile(r"<head>")
_ICON_LINK = re.compile(r'rel="icon"')

# What the mark is allowed to spend: the transfer ink and the sheet it prints on.
_TOKENS = ("accent", "raised")


def test_the_mark_is_well_formed_xml() -> None:
    try:
        ElementTree.fromstring(_FAVICON.read_text(encoding="utf-8"))
    except ElementTree.ParseError as broken:
        raise AssertionError(
            f"{_FAVICON.name} is not well-formed XML ({broken}). An SVG a browser refuses "
            "still serves 200 and still passes every other check here; what it draws is the "
            "broken-image glyph. The trap that caught this file first: an XML comment may "
            "not contain a double hyphen, and naming the accent custom property spells one."
        ) from broken


def test_the_mark_spends_only_palette_colours() -> None:
    declared = read_declared_colours()
    allowed = {declared[token]: token for token in _TOKENS}
    spent = {literal.lower() for literal in _SVG_COLOUR.findall(_FAVICON.read_text(encoding="utf-8"))}
    assert spent, f"no fill/stroke colour found in {_FAVICON} — this rule is vacuous"
    stray = sorted(spent - set(allowed))
    assert not stray, (
        f"{_FAVICON.name} spends {stray}, which palette.css does not declare as "
        f"{list(_TOKENS)}. The file cannot read var(--accent), so a literal here is the "
        "only option — but it has to be the token's current value, or the tab mark drifts "
        "away from the app it marks with nothing to report it."
    )


def test_every_template_that_owns_a_head_links_the_mark() -> None:
    heads = {path for path in _TEMPLATES.glob("*.html") if _HEAD.search(path.read_text(encoding="utf-8"))}
    assert heads, f"no <head> found under {_TEMPLATES} — this rule is vacuous"
    missing = sorted(
        path.name for path in heads if not _ICON_LINK.search(path.read_text(encoding="utf-8"))
    )
    assert not missing, (
        f"{missing} open a <head> and link no rel=\"icon\", so those pages show a blank "
        "tab. A standalone template repeats what base.html would have given it; the icon "
        "is part of that. Packet pages take the href as `icon` because theirs is relative."
    )
