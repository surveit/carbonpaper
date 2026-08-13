"""A `TableRef` becomes bytes in ONE place — app.evals.dataset — which is therefore the
only caller of `open_stored_file`, the read that names bytes with no project to scope
them. Everywhere else asks a project for its file. Inherited from the closed PR #612,
which owned the same chokepoint back when a TableRef held a path and the question was
which root it hung off.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_OWNER = "app/evals/dataset.py"
# The store's own module defines it; naming it here keeps the rule about REACH.
_DEFINER = "app/services/uploads.py"
_UNSCOPED_READ = "open_stored_file"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_only_the_owner_reads_the_store_without_a_project() -> None:
    offenders = [
        f"{_relative(path)}:{lineno}"
        for path in find_source_files_under(_REPO_ROOT / "app")
        if _relative(path) not in (_OWNER, _DEFINER)
        for lineno in find_unscoped_read_uses(parse_module(path))
    ]
    assert not offenders, (
        f"{_UNSCOPED_READ} fetches bytes by address alone, with no project to check them "
        f"against — {_OWNER} is where an eval dataset is resolved and the only place that "
        "may. Anywhere else, take the project id and call open_project_file, or go "
        f"through {_OWNER}:\n  " + "\n  ".join(offenders)
    )


def test_the_owner_still_reads_a_table_ref() -> None:
    """A rule nothing exercises is a rule that has stopped being about anything."""
    source = (_REPO_ROOT / _OWNER).read_text(encoding="utf-8")
    assert find_unscoped_read_uses(parse_module(_REPO_ROOT / _OWNER))
    assert "table.sha256" in source


def find_unscoped_read_uses(tree: ast.Module) -> list[int]:
    """Both spellings: the imported name, and the attribute reached through the module."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.ImportFrom, ast.Attribute))
        and _names_the_unscoped_read(node)
    )


def _names_the_unscoped_read(node: ast.ImportFrom | ast.Attribute) -> bool:
    if isinstance(node, ast.ImportFrom):
        return any(alias.name == _UNSCOPED_READ for alias in node.names)
    return node.attr == _UNSCOPED_READ


def _relative(path: Path) -> str:
    return path.relative_to(_REPO_ROOT).as_posix()


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_a_from_import_of_the_unscoped_read_is_flagged() -> None:
    tree = ast.parse("from app.services.uploads import open_stored_file\n")
    assert find_unscoped_read_uses(tree) == [1]


def test_reaching_it_through_the_module_is_flagged_too() -> None:
    tree = ast.parse("from app.services import uploads\nuploads.open_stored_file(sha)\n")
    assert find_unscoped_read_uses(tree) == [2]


def test_the_project_scoped_read_is_not_flagged() -> None:
    tree = ast.parse(
        "from app.services.uploads import open_project_file\n"
        "open_project_file(project_id, sha)\n"
    )
    assert find_unscoped_read_uses(tree) == []


@pytest.mark.parametrize("source", [
    "record = store.open_stored_files(sha)\n",
    "open_stored = 1\n",
])
def test_a_similar_name_is_not_flagged(source: str) -> None:
    assert find_unscoped_read_uses(ast.parse(source)) == []
