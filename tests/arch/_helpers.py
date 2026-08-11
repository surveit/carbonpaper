"""Static-analysis helpers for the architecture tests.

Nothing here imports the modules under test, so a violation surfaces as a plain
assertion naming the file rather than an import-time crash — and the tests run
against code that itself imports heavy optional dependencies.
"""
from __future__ import annotations

import ast
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "static"


def read_stylesheets() -> str:
    sheets = sorted(_STATIC_DIR.glob("*.css"))
    if not sheets:
        raise ValueError(f"no .css files under {_STATIC_DIR} — these rules would be vacuous")
    return "\n".join(sheet.read_text(encoding="utf-8") for sheet in sheets)


def parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def collect_called_funcs(tree: ast.Module) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


def collect_called_methods(tree: ast.Module) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def find_function_defs(tree: ast.Module) -> list[tuple[str, int]]:
    return [
        (node.name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def find_imported_modules(tree: ast.Module) -> set[str]:
    """Relative imports are skipped: same-package, they never cross an architecture boundary."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def find_dict_key_uses(tree: ast.Module, keys: set[str]) -> list[tuple[int, str]]:
    uses: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):
            target = node.slice
            if isinstance(target, ast.Constant) and target.value in keys:
                uses.append((node.lineno, str(target.value)))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value in keys
        ):
            uses.append((node.lineno, str(node.args[0].value)))
        elif isinstance(node, ast.Dict):
            for key_node in node.keys:
                if isinstance(key_node, ast.Constant) and key_node.value in keys:
                    uses.append((key_node.lineno, str(key_node.value)))
    return uses


def find_subclasses_of(tree: ast.Module, base_name: str) -> list[ast.ClassDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        and any(_base_name_matches(base, base_name) for base in node.bases)
    ]


def _base_name_matches(base: ast.expr, name: str) -> bool:
    if isinstance(base, ast.Name):
        return base.id == name
    if isinstance(base, ast.Attribute):
        return base.attr == name
    return False


def find_class_body_assignment(
    node: ast.ClassDef, name: str
) -> ast.Assign | ast.AnnAssign | None:
    for stmt in node.body:
        if (
            isinstance(stmt, ast.AnnAssign)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and stmt.value is not None
        ):
            return stmt
        if isinstance(stmt, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in stmt.targets
        ):
            return stmt
    return None


def find_class_body_function(
    node: ast.ClassDef, name: str
) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
            return stmt
    return None


def find_numeric_get_defaults(tree: ast.Module) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and not node.keywords
        ):
            continue
        default = node.args[1]
        if isinstance(default, ast.UnaryOp) and isinstance(default.op, ast.USub):
            default = default.operand  # negative literal, e.g. -1
        if (
            isinstance(default, ast.Constant)
            and isinstance(default.value, (int, float))
            and not isinstance(default.value, bool)
        ):
            spans.append((node.lineno, node.end_lineno or node.lineno))
    return spans
