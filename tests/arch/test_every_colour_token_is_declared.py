"""Architecture: every `var(--token)` resolves to a token something declares."""
from __future__ import annotations

import re
from pathlib import Path

from arch.vendored import VENDORED_SRI

_APP = Path(__file__).resolve().parents[2] / "app"
_SCANNED_SUFFIXES = {".css", ".html", ".js"}
_VENDORED = {f"static/{name}" for name in VENDORED_SRI}

_DECLARED = re.compile(r"(--[A-Za-z0-9_-]+)\s*:")
# `var(--x, fallback)` still has to name a token that exists.
_REFERENCED = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")


def find_undeclared_tokens() -> dict[str, list[str]]:
    """token -> the files reading it, for every token nothing under app/ declares."""
    declared: set[str] = set()
    referenced: dict[str, set[str]] = {}
    for path, text in _read_markup():
        declared.update(_DECLARED.findall(text))
        for token in _REFERENCED.findall(text):
            referenced.setdefault(token, set()).add(path)
    return {
        token: sorted(files)
        for token, files in sorted(referenced.items())
        if token not in declared
    }


def _read_markup() -> list[tuple[str, str]]:
    return [
        (rel, path.read_text(encoding="utf-8"))
        for path in sorted(_APP.rglob("*"))
        if path.is_file()
        and path.suffix in _SCANNED_SUFFIXES
        and (rel := path.relative_to(_APP).as_posix()) not in _VENDORED
    ]


def test_no_stylesheet_reads_a_token_nothing_declares() -> None:
    undeclared = find_undeclared_tokens()
    assert not undeclared, (
        f"custom properties read but never declared: {undeclared}. A missing token "
        "drops the whole declaration — a border becomes 0px wide and a background "
        "becomes transparent — so the element renders unstyled rather than falling "
        "back to anything. Declare it in app/static/palette.css, or point the use "
        "site at the token that already carries the role it wants."
    )


def test_the_scan_reads_the_files_it_is_meant_to_guard() -> None:
    scanned = dict(_read_markup())
    assert "static/palette.css" in scanned, "the palette itself is not being read"
    assert any(rel.endswith(".html") for rel in scanned), "no template is being read"
    assert _DECLARED.search("--accent: #1d539c;"), "the declaration pattern matches nothing"
    assert _REFERENCED.search("color: var(--accent);"), "the reference pattern matches nothing"
    # The rule has to fire on the shape it exists for, or it only ever passes.
    assert _REFERENCED.findall("border: 1px solid var(--never-declared);") == [
        "--never-declared"
    ]
