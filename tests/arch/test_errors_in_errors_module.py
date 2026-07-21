"""Architecture: exception classes live in a package's ``errors.py``.

An exception declared inline near its raise site scatters the error
vocabulary across the codebase; declaring it in a package's ``errors.py``
keeps one home per concept. Any package may have its own ``errors.py``
(``app/core/errors.py`` exists today; ``app/web/errors.py`` etc. are equally
valid homes) — the rule is only that the class must live in a file named
``errors.py``, not which one.

Detection is name-based, since AST can't resolve base classes to real types: a
``ClassDef`` counts as an exception iff one of its base names is "Exception",
"BaseException", or ends with "Error" or "Exception". This over-matches
nothing in practice for real exception hierarchies, and the allowlist below
absorbs any case where it does.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch._helpers import parse_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_EXEMPT_DIR_NAMES = {"_arch_tests", "__pycache__"}
_ERRORS_MODULE_NAME = "errors.py"

# Pre-existing classes that name-match the exception heuristic but cannot move
# into an errors.py today. A ratchet: new entries are forbidden — a new
# offender must be moved, not added here.
#
# - app/runtime/cancellation.py: RunCancelled — cancellation.py is a
#   stdlib-only leaf (app/runtime/_arch_tests/test_cancellation_is_a_stdlib_leaf.py):
#   it may not import any other app module, including a sibling errors.py, so
#   its one exception has to stay declared where it's raised.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {
        ("app/runtime/cancellation.py", "RunCancelled"),
    }
)


def find_exception_class_defs(tree: ast.Module) -> list[tuple[int, str]]:
    """(lineno, name) of every class in `tree` whose base names mark it as an
    exception — see the module docstring for the name-based heuristic."""
    return [
        (node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and any(
            _is_exception_base_name(name) for name in find_base_class_names(node)
        )
    ]


def find_base_class_names(node: ast.ClassDef) -> list[str]:
    """The simple (rightmost) name of each base class in a `ClassDef`: `Foo` ->
    "Foo", `mod.Foo` -> "Foo". Bases that are neither a plain name nor a dotted
    attribute (e.g. a subscripted generic) contribute no name and are skipped —
    real exception bases are never spelled that way."""
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def find_source_files(target: Path) -> list[Path]:
    """The .py files under `target` this rule governs: every non-exempt .py
    file below it (skipping _arch_tests/ and __pycache__)."""
    return sorted(
        path
        for path in target.rglob("*.py")
        if not any(part in _EXEMPT_DIR_NAMES for part in path.relative_to(target).parts)
    )


def _is_exception_base_name(name: str) -> bool:
    return name in ("Exception", "BaseException") or name.endswith(("Error", "Exception"))


def test_exception_classes_live_in_an_errors_module() -> None:
    offenders = [
        f"{path.relative_to(_REPO_ROOT).as_posix()}:{lineno}  class {name}"
        for path in find_source_files(_APP_ROOT)
        for lineno, name in find_exception_class_defs(parse_module(path))
        if (path.relative_to(_REPO_ROOT).as_posix(), name) not in _ALLOWLIST
        if path.name != _ERRORS_MODULE_NAME
    ]
    assert not offenders, (
        "exception classes must be declared in a package's errors.py, not "
        "inline near their raise site:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_base_class_names_reads_plain_name_base() -> None:
    tree = ast.parse("class Foo(ValueError):\n    pass\n")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert find_base_class_names(node) == ["ValueError"]


def test_find_base_class_names_reads_dotted_attribute_base() -> None:
    tree = ast.parse("class Foo(pydantic.ValidationError):\n    pass\n")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert find_base_class_names(node) == ["ValidationError"]


def test_find_base_class_names_skips_a_non_name_non_attribute_base() -> None:
    tree = ast.parse("class Foo(Generic[T]):\n    pass\n")
    (node,) = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert find_base_class_names(node) == []


def test_find_exception_class_defs_flags_exception_subclass() -> None:
    tree = ast.parse("class Boom(Exception):\n    pass\n")
    assert find_exception_class_defs(tree) == [(1, "Boom")]


def test_find_exception_class_defs_flags_base_exception_subclass() -> None:
    tree = ast.parse("class Boom(BaseException):\n    pass\n")
    assert find_exception_class_defs(tree) == [(1, "Boom")]


def test_find_exception_class_defs_flags_error_suffixed_base() -> None:
    tree = ast.parse("class BadInput(ValueError):\n    pass\n")
    assert find_exception_class_defs(tree) == [(1, "BadInput")]


def test_find_exception_class_defs_flags_exception_suffixed_base() -> None:
    tree = ast.parse("class BadInput(MyCustomException):\n    pass\n")
    assert find_exception_class_defs(tree) == [(1, "BadInput")]


def test_find_exception_class_defs_ignores_clean_snippet() -> None:
    tree = ast.parse(
        "class Stage(BaseModel):\n"
        "    pass\n\n"
        "class Handler:\n"
        "    pass\n"
    )
    assert find_exception_class_defs(tree) == []


def test_find_exception_class_defs_does_not_match_inside_a_word() -> None:
    """A base name ending in a banned word is a match; a base name merely
    CONTAINING one mid-word ("Errorless") is not, since `str.endswith` checks
    the tail, not an infix."""
    tree = ast.parse("class Foo(Errorless):\n    pass\n")
    assert find_exception_class_defs(tree) == []


def test_find_source_files_walks_directory_excluding_arch_tests(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.py").write_text("")
    (tmp_path / "_arch_tests").mkdir()
    (tmp_path / "_arch_tests" / "b.py").write_text("")
    assert {p.name for p in find_source_files(tmp_path)} == {"a.py"}


@pytest.mark.parametrize(
    "filename",
    ["errors.py"],
)
def test_exception_classes_live_in_an_errors_module_permits_errors_py(
    tmp_path: Path, filename: str
) -> None:
    """A file literally named errors.py is always a valid home, whatever
    package it sits in — the rule checks the filename only, not the path."""
    target = tmp_path / filename
    target.write_text("class Boom(Exception):\n    pass\n")
    offenders = [
        (lineno, name)
        for lineno, name in find_exception_class_defs(parse_module(target))
        if target.name != _ERRORS_MODULE_NAME
    ]
    assert offenders == []
