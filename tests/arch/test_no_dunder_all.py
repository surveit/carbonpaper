"""Architecture: no module declares ``__all__``. Nothing in this repo star-imports, so
``__all__`` is a hand-maintained second registry of every public name — one more list each
branch appends to, and one more merge conflict. To re-export from a package hub, use the
redundant-alias form ``from x import y as y``, which both Ruff and mypy accept as explicit.
Scope is every first-party ``.py`` file, tests included; the allowlist is empty.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import scan_all_text

_REPO_ROOT = Path(__file__).resolve().parents[2]


def find_dunder_all_assignments(tree: ast.Module) -> list[int]:
    """Line numbers of every statement in `tree` that assigns ``__all__``, at any nesting."""
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
        and _assigns_dunder_all(node)
    )


def find_dunder_all_offenders(paths: list[Path], repo_root: Path) -> list[str]:
    """"<path>:<lineno>" for every ``__all__`` assignment under `paths`."""
    return [
        f"{path.relative_to(repo_root).as_posix()}:{lineno}"
        for path in paths
        for lineno in find_dunder_all_assignments(parse_module(path))
    ]


def _assigns_dunder_all(node: ast.Assign | ast.AnnAssign | ast.AugAssign) -> bool:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(isinstance(target, ast.Name) and target.id == "__all__" for target in targets)


def test_no_module_declares_dunder_all() -> None:
    offenders = find_dunder_all_offenders(scan_all_text((".py",)), _REPO_ROOT)
    assert not offenders, (
        "__all__ is banned — nothing here star-imports, so it is only a second registry "
        "of public names to keep in sync. Delete it; to keep a package hub's re-export "
        "explicit for Ruff and mypy, write `from x import y as y`:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_find_dunder_all_assignments_flags_a_plain_assignment() -> None:
    assert find_dunder_all_assignments(ast.parse('__all__ = ["Foo"]\n')) == [1]


def test_find_dunder_all_assignments_flags_an_annotated_assignment() -> None:
    assert find_dunder_all_assignments(ast.parse('__all__: list[str] = ["Foo"]\n')) == [1]


def test_find_dunder_all_assignments_flags_an_augmented_assignment() -> None:
    assert find_dunder_all_assignments(ast.parse('__all__ += ["Foo"]\n')) == [1]


def test_find_dunder_all_assignments_flags_an_indented_assignment() -> None:
    assert find_dunder_all_assignments(ast.parse('if True:\n    __all__ = ["Foo"]\n')) == [2]


def test_find_dunder_all_assignments_ignores_a_mere_mention() -> None:
    """Naming ``__all__`` in a literal or an attribute access binds nothing, so it is legal."""
    tree = ast.parse('help = "see __all__"\nnames = mod.__all__\n')
    assert find_dunder_all_assignments(tree) == []


def test_find_dunder_all_assignments_ignores_a_clean_module() -> None:
    assert find_dunder_all_assignments(ast.parse("from x import y as y\n")) == []


def test_find_dunder_all_offenders_reports_repo_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "hub.py"
    target.write_text('__all__ = ["Foo"]\n', encoding="utf-8")
    assert find_dunder_all_offenders([target], tmp_path) == ["hub.py:1"]


def test_find_dunder_all_offenders_passes_a_redundant_alias_reexport(tmp_path: Path) -> None:
    target = tmp_path / "hub.py"
    target.write_text("from x import y as y\n", encoding="utf-8")
    assert find_dunder_all_offenders([target], tmp_path) == []
