"""Architecture: a project's identity is the `project` record in the document store,
and only that. `examples/<name>/project.json` was the second store; alembic revision
0010 copied it into records and nothing under ``app/`` may name it again — a reader
would revive a dual-write no code reconciles. Prose may still discuss the file; only
a string literal, which is what a real read or write needs, is an offence.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_DEAD_FILENAME = "project.json"


def find_dead_filename_literals(tree: ast.AST) -> list[int]:
    """Line numbers of every non-docstring string literal naming the dead file."""
    docstrings = {id(node) for node in _iter_docstring_nodes(tree)}
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _DEAD_FILENAME in node.value
        and id(node) not in docstrings
    )


def _iter_docstring_nodes(tree: ast.AST) -> list[ast.Constant]:
    found: list[ast.Constant] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.append(first.value)
    return found


def test_no_module_under_app_names_the_dead_project_file() -> None:
    offenders: list[str] = []
    for path in sorted(_APP_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{path.relative_to(_REPO_ROOT).as_posix()}:{line}"
            for line in find_dead_filename_literals(tree)
        ]
    assert not offenders, (
        f"{_DEAD_FILENAME} is dead data left on disk by alembic 0010, not a store: "
        "read the Project record instead of the file:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_separates_a_literal_from_prose() -> None:
    # Else the rule above passes on a detector that matches nothing.
    assert find_dead_filename_literals(ast.parse('p = pdir / "project.json"\n')) == [1]
    assert find_dead_filename_literals(ast.parse('x = {"f": "a/project.json"}\n')) == [1]
    assert find_dead_filename_literals(ast.parse('"""Once read from project.json."""\n')) == []
    assert find_dead_filename_literals(ast.parse('# reads project.json\nx = 1\n')) == []
