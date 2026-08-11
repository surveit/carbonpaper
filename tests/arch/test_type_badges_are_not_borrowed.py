"""Architecture: a stage-type badge class is emitted by TYPE_CLASS, never hand-written.

`index.html` painted a project's "Under development" with `.badge.llm` and "Live" with
`.badge.python`, so a project's lifecycle borrowed two stage types' classes. Those went
neutral when types lost their colour, and the chips silently turned grey.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.web.diagrams import TYPE_CLASS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIRS = (_REPO_ROOT / "app" / "templates", _REPO_ROOT / "app" / "web")
# The badge classes that DO mean a state, and are the right answer when a template
# wants a coloured chip of its own.
_STATE_CLASSES = ("ok", "warn", "err", "awaiting")


def find_hardcoded_type_badges() -> dict[str, list[str]]:
    pattern = re.compile(r'class="[^"]*\bbadge\s+(' + "|".join(sorted(set(TYPE_CLASS.values()))) + r')\b')
    found = {}
    for directory in _TEMPLATE_DIRS:
        for template in sorted(directory.rglob("*.html")):
            hits = pattern.findall(template.read_text(encoding="utf-8"))
            if hits:
                found[str(template.relative_to(_REPO_ROOT))] = sorted(set(hits))
    return found


def test_no_template_hardcodes_a_stage_type_badge_class() -> None:
    borrowed = find_hardcoded_type_badges()
    assert not borrowed, (
        f"{borrowed} write a stage-type badge class by hand. Those classes carry no "
        "colour — a type is not a state — so a chip that borrows one to mean something "
        f"else renders neutral and the meaning is lost. Use one of {_STATE_CLASSES} when "
        "the chip means a state; a real stage type gets its class from TYPE_CLASS."
    )


def test_the_scan_would_see_a_borrowed_class() -> None:
    assert TYPE_CLASS, "TYPE_CLASS is empty — the pattern would match nothing"
    assert all(d.is_dir() for d in _TEMPLATE_DIRS), f"{_TEMPLATE_DIRS} — templates moved"
