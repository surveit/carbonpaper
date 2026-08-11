"""Architecture: app/static/palette.css is the only place a colour may be written.

Before this rule the app spent 172 distinct literals on about 40 decisions — four
amber tints, three greens for "good", twenty-four pale blues — because each surface
picked its own. A token cannot drift; a literal beside it can.
"""
from __future__ import annotations

import re
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_PALETTE = _APP / "static" / "palette.css"

# A mermaid `style` line takes a literal hex and cannot read a custom property, so
# this file repeats the palette in Python. Every literal in it is pinned back to the
# property it copies by tests/arch/test_status_colour_contract.py, which is the price
# of the exemption — do not add a file here that no rule pins.
_PINNED_ELSEWHERE = {"web/diagrams.py"}

_MARKUP_SUFFIXES = {".css", ".html", ".js"}
_LITERAL = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\([^)]*\)|\bhsla?\([^)]*\)")
# A bare colour keyword counts too — `background: white` was 26 of the old spellings.
_KEYWORD = re.compile(
    r"(?:background|color|border[a-z-]*|fill|stroke|outline[a-z-]*)\s*:[^;{}\n]*"
    r"\b(white|black|red|green|blue|orange|yellow|purple|pink|teal|navy|maroon|"
    r"gray|grey|silver|gold|beige|ivory|cream)\b"
)
# In Python a colour only ever appears as a CSS/mermaid property inside a string;
# a bare `#357` in prose is an issue reference, not a colour.
_PYTHON_LITERAL = re.compile(r"(?:fill|stroke|color|background|border)[a-z-]*\s*:\s*(#[0-9a-fA-F]{3,8})")


def find_colour_literals() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in sorted(_APP.rglob("*")):
        rel = path.relative_to(_APP).as_posix()
        if not path.is_file() or path == _PALETTE or rel in _PINNED_ELSEWHERE:
            continue
        text = _read_scannable(path)
        if text is None:
            continue
        pattern = _LITERAL if path.suffix in _MARKUP_SUFFIXES else _PYTHON_LITERAL
        spent = pattern.findall(text) + [m[0] if isinstance(m, tuple) else m
                                         for m in _KEYWORD.findall(text)]
        if spent:
            found[rel] = sorted({s if isinstance(s, str) else s[0] for s in spent})
    return found


def _read_scannable(path: Path) -> str | None:
    if path.suffix not in _MARKUP_SUFFIXES | {".py"}:
        return None
    return path.read_text(encoding="utf-8")


def test_no_file_but_the_palette_writes_a_colour() -> None:
    stray = find_colour_literals()
    assert not stray, (
        f"colour literals outside {_PALETTE.name}: {stray}. Every colour the app spends "
        "is declared once in app/static/palette.css and referenced as var(--token); a "
        "literal here is the drift that file exists to end. If the value you need has no "
        "token, the honest fix is a new token with the dimension it adds written down, "
        "not a hex at the use site."
    )


def test_the_scan_reaches_the_stylesheet_it_is_meant_to_guard() -> None:
    sheets = sorted((_APP / "static").glob("*.css"))
    assert sheets, "no stylesheet under app/static is being read — the rule is vacuous"
    for sheet in sheets:
        assert _read_scannable(sheet), f"{sheet} is not being read — the rule is vacuous"
    assert _LITERAL.search("border: 1px solid #ddd8ce;"), "the literal pattern matches no hex"
    assert _KEYWORD.search("background: white;"), "the keyword pattern matches no colour name"
    assert _PYTHON_LITERAL.search('"fill:#f8f6f1"'), "the python pattern matches no mermaid fill"
