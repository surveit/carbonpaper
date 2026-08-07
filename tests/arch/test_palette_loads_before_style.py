"""A page linking style.css must link palette.css first — style.css spends those
properties and defines none of them. An undefined var() drops its whole
declaration, so `border-top: 1px solid var(--border)` renders as no border rather
than a broken one: lineage.html lost every frame this way and looked designed.
"""
from __future__ import annotations

from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
PALETTE = "/static/palette.css"
STYLE = "/static/style.css"


def _templates_linking(href: str) -> list[Path]:
    return [
        path
        for path in sorted(TEMPLATES.glob("*.html"))
        if f'rel="stylesheet" href="{href}"' in path.read_text(encoding="utf-8")
    ]


def test_every_page_linking_style_links_the_palette_first() -> None:
    pages = _templates_linking(STYLE)
    assert pages, "no template links style.css — has the stylesheet moved?"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        assert PALETTE in text, (
            f"{page.name} links style.css but not palette.css. Every var(--…) it "
            "spends resolves to nothing, and each declaration using one is dropped."
        )
        assert text.index(PALETTE) < text.index(STYLE), (
            f"{page.name} links palette.css after style.css. The palette defines the "
            "properties style.css reads, so it has to come first."
        )
