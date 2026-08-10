"""A page must load palette.css before any sheet that spends its tokens.

An undefined var() drops its whole declaration, so `1px solid var(--border)` renders
as no border rather than a broken one: lineage.html lost every frame this way and
looked designed. One list owns the order; no page may spell out its own.
"""
from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
PARTIAL = TEMPLATES / "_stylesheets.html"
PALETTE = "palette.css"
_STATIC_HREF = re.compile(r'rel="stylesheet" href="/static/([^"]+)"')


def test_the_partial_links_the_palette_first() -> None:
    linked = _STATIC_HREF.findall(PARTIAL.read_text(encoding="utf-8"))
    assert linked, f"{PARTIAL.name} links no stylesheet — has the list moved?"
    assert linked[0] == PALETTE, (
        f"{PARTIAL.name} links {linked[0]} before {PALETTE}. The palette defines the "
        "properties every other sheet reads, so it has to come first; each declaration "
        "spending a var(--…) declared later is dropped."
    )


def test_no_page_links_a_stylesheet_around_the_partial() -> None:
    """A page spelling out its own <link> order is how a sheet gets loaded before the palette."""
    strays = {
        path.name: sorted(set(_STATIC_HREF.findall(path.read_text(encoding="utf-8"))))
        for path in sorted(TEMPLATES.glob("*.html"))
        if path != PARTIAL and _STATIC_HREF.search(path.read_text(encoding="utf-8"))
    }
    assert not strays, (
        f"{strays} link a stylesheet directly instead of including _stylesheets.html. "
        "That file is the one place the cascade order is written down, and the review "
        "packet reads its concatenation order out of it — a page that goes around it "
        "gets an order nothing checks."
    )


def test_some_page_actually_includes_the_partial() -> None:
    """Without this the two rules above pass on a partial no page loads."""
    including = [
        path.name
        for path in sorted(TEMPLATES.glob("*.html"))
        if '{% include "_stylesheets.html" %}' in path.read_text(encoding="utf-8")
    ]
    assert including, "no template includes _stylesheets.html — no page has any CSS at all"
