"""Architecture: exception classes live in a file named ``errors.py`` — any package's,
not one specific module. Detection is name-based, since AST can't resolve base classes
to real types: a ``ClassDef`` counts as an exception iff one of its base names is
"Exception", "BaseException", or ends with "Error" or "Exception".
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_ERRORS_MODULE_NAME = "errors.py"

# Pre-existing classes that name-match the exception heuristic but cannot move
# into an errors.py today. A ratchet: new entries are forbidden — a new
# offender must be moved, not added here.
_ALLOWLIST: frozenset[tuple[str, str]] = frozenset()


def find_exception_class_defs(tree: ast.Module) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and any(
            _is_exception_base_name(name) for name in find_base_class_names(node)
        )
    ]


def find_base_class_names(node: ast.ClassDef) -> list[str]:
    names: list[str] = []
    for base in node.bases:
        if isinstance(base, ast.Name):
            names.append(base.id)
        elif isinstance(base, ast.Attribute):
            names.append(base.attr)
    return names


def _is_exception_base_name(name: str) -> bool:
    return name in ("Exception", "BaseException") or name.endswith(("Error", "Exception"))


def _is_valid_errors_home(path: Path) -> bool:
    return path.name == _ERRORS_MODULE_NAME


def find_errors_module_offenders(paths: list[Path], repo_root: Path) -> list[str]:
    return [
        f"{path.relative_to(repo_root).as_posix()}:{lineno}  class {name}"
        for path in paths
        if not _is_valid_errors_home(path)
        for lineno, name in find_exception_class_defs(parse_module(path))
        if (path.relative_to(repo_root).as_posix(), name) not in _ALLOWLIST
    ]


def test_exception_classes_live_in_an_errors_module() -> None:
    offenders = find_errors_module_offenders(find_source_files_under(_APP_ROOT), _REPO_ROOT)
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
    tree = ast.parse("class Foo(Errorless):\n    pass\n")
    assert find_exception_class_defs(tree) == []


def test_is_valid_errors_home_accepts_errors_py() -> None:
    assert _is_valid_errors_home(Path("app/core/errors.py")) is True


def test_is_valid_errors_home_rejects_other_filename() -> None:
    assert _is_valid_errors_home(Path("app/core/exceptions.py")) is False


def test_find_errors_module_offenders_permits_an_exception_in_errors_py(tmp_path: Path) -> None:
    target = tmp_path / "errors.py"
    target.write_text("class Boom(Exception):\n    pass\n", encoding="utf-8")
    assert find_errors_module_offenders([target], tmp_path) == []


def test_find_errors_module_offenders_flags_an_exception_outside_errors_py(tmp_path: Path) -> None:
    target = tmp_path / "other.py"
    target.write_text("class Boom(Exception):\n    pass\n", encoding="utf-8")
    assert find_errors_module_offenders([target], tmp_path) == ["other.py:1  class Boom"]
