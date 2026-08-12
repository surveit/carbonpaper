"""Architecture: the agent mark is drawn in one place, `_sparkle.html`.
Typing ✨ instead of calling the macro fails silently — a font paints the emoji in its
own colours and ignores `fill`. WHICH surfaces deserve the mark is not knowable from a
template and is not tested; see docs/visual-language.md.
"""
from __future__ import annotations

import re
from pathlib import Path

_TEMPLATES = Path(__file__).resolve().parents[2] / "app" / "templates"
_MACRO = "_sparkle.html"


def read_markup() -> dict[str, str]:
    paths = sorted(_TEMPLATES.glob("*.html"))
    if not paths:
        raise ValueError(f"no templates under {_TEMPLATES} — this rule would be vacuous")
    return {p.name: re.sub(r"\{#.*?#\}", "", p.read_text(encoding="utf-8"), flags=re.S)
            for p in paths}


def find_templates_spelling_the_character(markup: dict[str, str]) -> list[str]:
    return sorted(name for name, text in markup.items() if "✨" in text)


def test_the_agent_mark_has_one_source() -> None:
    markup = read_markup()
    assert "<svg" in markup[_MACRO], f"{_MACRO} draws no svg — has the mark moved?"
    spelled = find_templates_spelling_the_character(markup)
    assert not spelled, (
        f"{spelled} spell ✨ in markup. A font paints the emoji in its own colours and "
        f"ignores `fill`, so it cannot be the transfer ink — call {_MACRO}'s `sparkle()`. "
        "(app/web/diagrams.py keeps the character: a mermaid node label holds no markup.)"
    )
