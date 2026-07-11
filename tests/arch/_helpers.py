"""Static-analysis helpers for the architecture tests.

Each helper reads a source file and inspects its AST. Nothing here imports the
modules under test, so a boundary violation surfaces as a plain assertion naming
the file rather than an import-time crash — and the tests run fine against code
that itself imports heavy optional dependencies.
"""
from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP = _REPO_ROOT / "app"


def iter_module_files(package: str) -> Iterator[Path]:
    """Yield each .py file under app/<package>.

    `package` is a path relative to app/ ("agent", "compiler/agent"); "" or "."
    means the whole app tree.
    """
    root = _APP if package in ("", ".") else _APP / package
    if not root.exists():
        raise FileNotFoundError(f"architecture test targets a missing package: {root}")
    yield from sorted(root.rglob("*.py"))


def parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def collect_imports(tree: ast.Module) -> set[str]:
    """Return the module names named by `import x` and `from x import ...`."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def collect_called_funcs(tree: ast.Module) -> set[str]:
    """Return the names of calls to a bare function, e.g. `open(...)` -> {"open"}."""
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def collect_called_methods(tree: ast.Module) -> set[str]:
    """Return the attribute names of method calls, e.g. `p.write_text()` -> {"write_text"}."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
