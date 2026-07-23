"""Architecture: no HTML-document tags in a Python string literal.

HTML belongs in ``app/templates/*.html``, rendered through the templating
layer; inline HTML built inside a Python string literal was a real review
finding — it means a page fragment was authored in the wrong layer, outside
the templates a reviewer would look at for markup.

Detection is AST-based: every string-literal ``ast.Constant`` in a module
(this also walks the literal segments of an f-string, since those parse as
nested ``Constant`` nodes) is searched for one of a conservative, explicit
tag list: ``<html``, ``<div``, ``<table``, ``<body``, ``<span``, ``<ul``,
``<li``, ``<form``, ``<button``. Matching is case-insensitive and requires a
non-identifier character (or end of string) right after the tag name, so
``<li`` matches ``<li>``/``<li class=...>`` but not ``<list ...>`` — a
compiler prompt file (``app/compiler/prompt.py``) legitimately writes the
placeholder ``<list of stage dicts as above>``, and a naive substring search
would misflag it. The tag list is intentionally narrow and never matches a
generic ``<word>`` pattern such as ``<example>`` or ``<methodology>``, which
compiler prompt files use as XML-ish delimiters, not as HTML.

Scope is ``app/`` (excluding ``tests/`` and ``_arch_tests/``, which hold the
checker's own inline test-fixture snippets).
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

_BANNED_TAGS = ("html", "div", "table", "body", "span", "ul", "li", "form", "button")
_HTML_TAG_PATTERN = re.compile(
    r"<(?:" + "|".join(_BANNED_TAGS) + r")(?![a-zA-Z0-9_])", re.IGNORECASE
)

# Pre-existing offenders that are not the page-fragment-in-Python leak this
# rule targets. A ratchet: new entries are forbidden — a new offender must be
# fixed, not added here.
#
# - app/web/diagrams.py:175,344,346: a Mermaid flowchart node label embeds a
#   `<span style=...>` fragment for in-node text styling. Mermaid.js reads
#   this as diagram source, rendered client-side by the Mermaid library, not
#   as a page fragment produced by this app's own Jinja templates — there is
#   no app/templates/*.html this markup could move into.
_ALLOWLIST: frozenset[tuple[str, int]] = frozenset(
    {
        ("app/web/diagrams.py", 175),
        ("app/web/diagrams.py", 344),
        ("app/web/diagrams.py", 346),
    }
)


def find_html_tag_string_literals(tree: ast.Module) -> list[tuple[int, str]]:
    """(lineno, text) for every string-literal constant in `tree` whose text
    contains one of the banned HTML-document tags (see module docstring)."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _HTML_TAG_PATTERN.search(node.value):
                offenders.append((node.lineno, node.value))
    return offenders


def test_no_html_tags_in_python_string_literals() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  {text!r}"
        for path in find_source_files_under(_APP_ROOT)
        for lineno, text in find_html_tag_string_literals(parse_module(path))
        if (path.relative_to(_REPO_ROOT).as_posix(), lineno) not in _ALLOWLIST
    ]
    assert not offenders, (
        "HTML belongs in app/templates/*.html, not a Python string literal — "
        "a real review finding:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


@pytest.mark.parametrize("tag", _BANNED_TAGS)
def test_find_html_tag_string_literals_flags_each_tag_individually(tag: str) -> None:
    tree = ast.parse(f'x = "<{tag}>"\n')
    assert find_html_tag_string_literals(tree) == [(1, f"<{tag}>")]


def test_find_html_tag_string_literals_flags_tag_with_attributes() -> None:
    tree = ast.parse('x = "<div class=\'card\'>"\n')
    assert find_html_tag_string_literals(tree) == [(1, "<div class='card'>")]


def test_find_html_tag_string_literals_is_case_insensitive() -> None:
    tree = ast.parse('x = "<DIV>"\n')
    assert find_html_tag_string_literals(tree) == [(1, "<DIV>")]


def test_find_html_tag_string_literals_flags_tag_inside_an_fstring() -> None:
    tree = ast.parse('name = "x"\nlabel = f"<span>{name}</span>"\n')
    assert find_html_tag_string_literals(tree) == [(2, "<span>")]


def test_find_html_tag_string_literals_ignores_a_longer_word_sharing_the_tag_prefix() -> None:
    """"<list of stage dicts as above>" is a compiler-prompt placeholder, not
    a `<li>` tag — a naive substring search for "<li" would wrongly flag it,
    which is exactly the false positive this rule must avoid."""
    tree = ast.parse('x = "<list of stage dicts as above>"\n')
    assert find_html_tag_string_literals(tree) == []


def test_find_html_tag_string_literals_ignores_generic_xml_ish_tag() -> None:
    tree = ast.parse('x = "<example>done</example>"\n')
    assert find_html_tag_string_literals(tree) == []


def test_find_html_tag_string_literals_ignores_clean_snippet() -> None:
    tree = ast.parse('def render(name: str) -> str:\n    return f"hello {name}"\n')
    assert find_html_tag_string_literals(tree) == []
