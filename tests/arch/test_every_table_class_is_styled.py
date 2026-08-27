"""Architecture: a class on a <table> must match a rule in some stylesheet."""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_TEMPLATES = _APP / "templates"

_TABLE_TAG = re.compile(r"<table[^>]*\sclass=\"([^\"]*)\"")
_STYLE_BLOCK = re.compile(r"<style[^>]*>(.*?)</style>", re.S)
_JINJA = re.compile(r"\{[%{].*?[%}]\}", re.S)
_PLAIN_CLASS = re.compile(r"[a-z][a-z0-9-]*")


def read_every_stylesheet() -> str:
    sheets = [path.read_text(encoding="utf-8") for path in sorted((_APP / "static").glob("*.css"))]
    inline = [
        block
        for path in sorted(_TEMPLATES.glob("*.html"))
        for block in _STYLE_BLOCK.findall(path.read_text(encoding="utf-8"))
    ]
    return "\n".join(sheets + inline)


def find_table_classes() -> dict[str, str]:
    seen: dict[str, str] = {}
    for path in sorted(_TEMPLATES.glob("*.html")):
        for attribute in _TABLE_TAG.findall(path.read_text(encoding="utf-8")):
            for name in _PLAIN_CLASS.findall(_JINJA.sub(" ", attribute)):
                seen.setdefault(name, path.name)
    return seen


def find_unstyled_table_classes() -> dict[str, str]:
    styles = read_every_stylesheet()
    return {
        name: template
        for name, template in find_table_classes().items()
        if not re.search(rf"\.{re.escape(name)}(?![-\w])", styles)
    }


def test_every_table_class_matches_a_rule() -> None:
    unstyled = find_unstyled_table_classes()
    assert not unstyled, (
        f"table classes no stylesheet rules on: {unstyled}. A class matching nothing "
        "renders as a bare browser table, which looks deliberate — reach for the shape "
        "whose page role matches (.stages for a list, .schema for config in a pane, "
        ".data-preview for rows) or write the rule. A class carried only to be found by "
        "script is a js- hook, not a table class."
    )


def test_the_scan_finds_the_tables_and_the_rules_it_is_meant_to_pair() -> None:
    classes = find_table_classes()
    assert len(classes) > 10, f"only {len(classes)} table classes found — the rule is vacuous"
    for known in ("stages", "schema", "data-preview"):
        assert known in classes, f".{known} is not being found — the scan misses templates"
    styles = read_every_stylesheet()
    assert "table.stages" in styles, "app/static is not being read — the rule is vacuous"
    assert "table.lin-row" in styles, "template <style> blocks are not being read"
