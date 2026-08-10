"""Architecture: text-mode file IO names its encoding. Python's default is the
platform's locale codec, so a template holding `·` or `→` reads fine on Linux CI
and raises UnicodeDecodeError on a Windows checkout. Binary mode takes no encoding.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch._helpers import parse_module
from arch.scope import scan_all_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TEXT_IO_METHODS = frozenset({"read_text", "write_text"})


def find_encodingless_text_io(tree: ast.Module) -> list[tuple[int, str]]:
    offenders: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = _callee_name(node)
        if callee is None or _passes_keyword(node, "encoding"):
            continue
        if callee in _TEXT_IO_METHODS or (callee == "open" and not _opens_binary(node)):
            offenders.append((node.lineno, callee))
    return offenders


def test_no_text_file_io_without_an_explicit_encoding() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  {callee}()"
        for path in scan_all_text((".py",))
        for lineno, callee in find_encodingless_text_io(parse_module(path))
    ]
    assert not offenders, (
        "text-mode file IO must pass encoding=\"utf-8\" — the default is the "
        "platform locale codec, which breaks on non-ASCII outside Linux:\n  "
        + "\n  ".join(offenders)
    )


def _callee_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _passes_keyword(node: ast.Call, name: str) -> bool:
    return any(keyword.arg == name for keyword in node.keywords)


def _opens_binary(node: ast.Call) -> bool:
    """An unprovable mode counts as text: fail loud rather than assume binary."""
    index = 1 if isinstance(node.func, ast.Name) else 0  # builtin open(file, mode); bound Path.open(mode)
    mode: ast.expr | None = node.args[index] if len(node.args) > index else None
    for keyword in node.keywords:
        if keyword.arg == "mode":
            mode = keyword.value
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


# --- unit tests for the checker, on inline snippets (red + green) ---------


@pytest.mark.parametrize("method", sorted(_TEXT_IO_METHODS))
def test_find_encodingless_text_io_flags_each_path_text_method(method: str) -> None:
    tree = ast.parse(f"p.{method}()\n")
    assert find_encodingless_text_io(tree) == [(1, method)]


@pytest.mark.parametrize("method", sorted(_TEXT_IO_METHODS))
def test_find_encodingless_text_io_allows_each_path_text_method_with_encoding(
    method: str,
) -> None:
    tree = ast.parse(f'p.{method}(encoding="utf-8")\n')
    assert find_encodingless_text_io(tree) == []


def test_find_encodingless_text_io_flags_bare_open_with_no_mode() -> None:
    tree = ast.parse('open("f")\n')
    assert find_encodingless_text_io(tree) == [(1, "open")]


def test_find_encodingless_text_io_flags_open_in_a_text_mode() -> None:
    tree = ast.parse('open("f", "w")\n')
    assert find_encodingless_text_io(tree) == [(1, "open")]


def test_find_encodingless_text_io_flags_path_open_method() -> None:
    tree = ast.parse('p.open("w")\n')
    assert find_encodingless_text_io(tree) == [(1, "open")]


def test_find_encodingless_text_io_allows_binary_path_open_method() -> None:
    tree = ast.parse('p.open("rb")\n')
    assert find_encodingless_text_io(tree) == []


def test_find_encodingless_text_io_allows_open_with_encoding() -> None:
    tree = ast.parse('open("f", "w", encoding="utf-8")\n')
    assert find_encodingless_text_io(tree) == []


@pytest.mark.parametrize("mode", ["rb", "wb", "ab", "r+b"])
def test_find_encodingless_text_io_allows_binary_open(mode: str) -> None:
    tree = ast.parse(f'open("f", "{mode}")\n')
    assert find_encodingless_text_io(tree) == []


def test_find_encodingless_text_io_allows_binary_open_via_mode_keyword() -> None:
    tree = ast.parse('open("f", mode="rb")\n')
    assert find_encodingless_text_io(tree) == []


def test_find_encodingless_text_io_flags_open_whose_mode_is_not_a_literal() -> None:
    tree = ast.parse('open("f", mode)\n')
    assert find_encodingless_text_io(tree) == [(1, "open")]


def test_find_encodingless_text_io_ignores_a_clean_module() -> None:
    tree = ast.parse('from pathlib import Path\nPath("f").read_text(encoding="utf-8")\n')
    assert find_encodingless_text_io(tree) == []
