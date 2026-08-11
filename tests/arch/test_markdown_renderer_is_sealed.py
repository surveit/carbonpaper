"""Architecture: one Markdown renderer, built with raw HTML off, and no template that
turns autoescape off. Assistant chat text quotes untrusted rows, so the three ways it
could reach a browser as live markup — a second MarkdownIt somewhere, an html=True on
this one, a ``|safe`` in a template — each fail here rather than in review.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch._helpers import parse_module
from arch.scope import scan_all_source, scan_all_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SHARED_RENDERER = "app/web/markdown_render.py"
_MARKDOWN_PACKAGE = "markdown_it"
_CONSTRUCTOR = "MarkdownIt"
_HTML_OPTION = "html"
# markdown-it-py's default preset. It sets html=True and passes raw HTML through, so a
# construction that names it — or names nothing — is the failure this rule exists for.
_HTML_ENABLING_PRESET = "commonmark"
_AUTOESCAPE_BYPASSES = ("|safe", "| safe", "autoescape false")


def test_only_the_shared_renderer_imports_the_markdown_package() -> None:
    offenders = [
        _relative(path)
        for path in scan_all_source()
        if _relative(path) != _SHARED_RENDERER
        and _MARKDOWN_PACKAGE in _read_imported_roots(parse_module(path))
    ]
    assert not offenders, (
        f"{_MARKDOWN_PACKAGE} may only be imported by {_SHARED_RENDERER} — a second "
        "renderer would carry its own options and drift out of the ones this rule "
        "pins. Import render_markdown instead:\n  " + "\n  ".join(offenders)
    )


def test_no_source_file_enables_raw_html() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}  {reason}"
        for path in scan_all_source()
        for lineno, reason in find_raw_html_enablers(parse_module(path))
    ]
    assert not offenders, (
        "raw HTML must stay OFF: with html=True a filing or a scraped page quoted in "
        "an assistant reply reaches the browser as live markup. Escape it instead of "
        "passing it through:\n  " + "\n  ".join(offenders)
    )


def test_every_renderer_construction_pins_raw_html_off() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}"
        for path in scan_all_source()
        for lineno in find_constructions_missing_html_off(parse_module(path))
    ]
    assert not offenders, (
        f"a {_CONSTRUCTOR}(...) must pass {_HTML_OPTION}=False explicitly — omitting it "
        f"takes the {_HTML_ENABLING_PRESET} preset's html=True by default, which is the "
        "silent way this protection is lost:\n  " + "\n  ".join(offenders)
    )


def test_no_template_switches_autoescape_off() -> None:
    offenders = [
        f"{_relative(path)}  {bypass}"
        for path in scan_all_text((".html",))
        if _relative(path).startswith("app/")
        for bypass in _AUTOESCAPE_BYPASSES
        if bypass in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "a template must not switch off autoescape: the only unescaped HTML on a chat "
        f"page is Markup from {_SHARED_RENDERER}, which needs no bypass. Escape in "
        "Python, where it is reviewable:\n  " + "\n  ".join(offenders)
    )


def find_raw_html_enablers(tree: ast.Module) -> list[tuple[int, str]]:
    """(lineno, reason) for each place raw HTML is switched back on."""
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == _HTML_OPTION and _is_true(node.value):
            offenders.append((node.value.lineno, f"{_HTML_OPTION}=True keyword"))
        elif isinstance(node, ast.Dict):
            offenders.extend(_find_true_html_entries(node))
        elif isinstance(node, ast.Assign) and _is_true(node.value):
            offenders.extend(
                (node.lineno, f'["{_HTML_OPTION}"] = True store')
                for target in node.targets
                if _is_html_subscript(target)
            )
        elif isinstance(node, ast.Call) and _names_html_enabling_preset(node):
            offenders.append((node.lineno, f'{_CONSTRUCTOR}("{_HTML_ENABLING_PRESET}") preset'))
    return offenders


def find_constructions_missing_html_off(tree: ast.Module) -> list[int]:
    """Line of each ``MarkdownIt(...)`` call that never says ``html`` is False."""
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _is_renderer_construction(node)
        and not _pins_html_off(node)
    ]


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


def _read_imported_roots(tree: ast.Module) -> set[str]:
    """Top-level package of every import, so ``markdown_it.rules_core`` counts too."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            roots.add(node.module.split(".")[0])
    return roots


def _is_true(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _is_html_subscript(target: ast.expr) -> bool:
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.slice, ast.Constant)
        and target.slice.value == _HTML_OPTION
    )


def _find_true_html_entries(node: ast.Dict) -> list[tuple[int, str]]:
    return [
        (key.lineno, f'"{_HTML_OPTION}": True entry')
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and key.value == _HTML_OPTION and _is_true(value)
    ]


def _is_renderer_construction(node: ast.Call) -> bool:
    return isinstance(node.func, ast.Name) and node.func.id == _CONSTRUCTOR


def _names_html_enabling_preset(node: ast.Call) -> bool:
    if not (_is_renderer_construction(node) and node.args):
        return False
    first = node.args[0]
    return isinstance(first, ast.Constant) and first.value == _HTML_ENABLING_PRESET


def _pins_html_off(node: ast.Call) -> bool:
    """True when the call says ``html`` is False, as a keyword or a dict entry."""
    for keyword in node.keywords:
        if keyword.arg == _HTML_OPTION and _is_false(keyword.value):
            return True
    return any(_holds_html_off(argument) for argument in [*node.args, *_keyword_values(node)])


def _keyword_values(node: ast.Call) -> list[ast.expr]:
    return [keyword.value for keyword in node.keywords]


def _holds_html_off(node: ast.expr) -> bool:
    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == _HTML_OPTION and _is_false(value)
        for key, value in zip(node.keys, node.values)
    )


def _is_false(node: ast.expr) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


# --- unit tests for the checkers, on inline snippets (red + green) ---------


@pytest.mark.parametrize(
    "source",
    [
        'MarkdownIt("js-default", {"html": True})',
        'MarkdownIt("js-default", html=True)',
        'md.options["html"] = True',
        'MarkdownIt("commonmark", {"html": False})',
    ],
)
def test_find_raw_html_enablers_flags_each_way_of_turning_it_on(source: str) -> None:
    assert find_raw_html_enablers(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        'MarkdownIt("js-default", {"html": False})',
        'MarkdownIt("zero", {"html": False, "linkify": True})',
        'options = {"linkify": True}',
    ],
)
def test_find_raw_html_enablers_passes_a_renderer_with_it_off(source: str) -> None:
    assert find_raw_html_enablers(ast.parse(source)) == []


def test_find_raw_html_enablers_ignores_an_unrelated_true_flag() -> None:
    assert find_raw_html_enablers(ast.parse('cfg = {"linkify": True, "breaks": True}')) == []


@pytest.mark.parametrize(
    "source",
    ["MarkdownIt()", 'MarkdownIt("js-default")', 'MarkdownIt("js-default", {"linkify": True})'],
)
def test_find_constructions_missing_html_off_flags_an_unpinned_construction(source: str) -> None:
    assert find_constructions_missing_html_off(ast.parse(source)) == [1]


@pytest.mark.parametrize(
    "source",
    [
        'MarkdownIt("js-default", {"html": False})',
        'MarkdownIt("js-default", options_update={"html": False})',
        'MarkdownIt("js-default", html=False)',
    ],
)
def test_find_constructions_missing_html_off_passes_a_pinned_construction(source: str) -> None:
    assert find_constructions_missing_html_off(ast.parse(source)) == []


def test_find_constructions_missing_html_off_ignores_a_call_that_is_not_the_renderer() -> None:
    assert find_constructions_missing_html_off(ast.parse('Template("x", {"linkify": True})')) == []


def test_the_shared_renderer_is_where_this_rule_says_it_is() -> None:
    """A moved or renamed renderer must fail here, not silently empty the rule."""
    assert (_REPO_ROOT / _SHARED_RENDERER).is_file()
