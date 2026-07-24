"""Static-analysis helpers for the architecture tests.

Each helper reads a source file and inspects its AST. Nothing here imports the
modules under test, so a boundary violation surfaces as a plain assertion naming
the file rather than an import-time crash — and the tests run fine against code
that itself imports heavy optional dependencies.
"""
from __future__ import annotations

import ast
from pathlib import Path


def parse_module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


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


def find_function_defs(tree: ast.Module) -> list[tuple[str, int]]:
    """(name, lineno) of every function or async-function definition."""
    return [
        (node.name, node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def find_imported_modules(tree: ast.Module) -> set[str]:
    """Dotted names this module imports: `import a.b` and `from a.b import c` both
    yield "a.b". Relative imports (`from . import x`) are skipped — they are
    same-package and never cross an architecture boundary."""
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None and node.level == 0:
            modules.add(node.module)
    return modules


def find_imported_names(tree: ast.Module) -> set[str]:
    """The names a module's imports bind, as written at the source: the member
    name of each `from X import name` (the `name` itself, ignoring any `as`
    alias — `from m import Foo as Bar` yields "Foo"), plus the dotted module
    name of each plain `import a.b` ("a.b"). Aliasing cannot hide the imported
    name, so a rule that bans importing a given symbol catches the aliased form
    too. This is distinct from `find_imported_modules`, which returns the module
    a name is imported FROM (the `X` in `from X import name`); this returns the
    bound member name (`name`) instead."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def find_dict_key_uses(tree: ast.Module, keys: set[str]) -> list[tuple[int, str]]:
    """(lineno, key) of each place the module reads or writes one of `keys` as a
    dict key: a subscript (`x["path"]`), a `.get("path", ...)` first argument, or
    a dict-literal key (`{"path": ...}`). String constants elsewhere — docstrings,
    messages, comparisons — do not count: the rule is about touching the keyed
    data, not about mentioning the word."""
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
    """`ClassDef` nodes in `tree` with a base named `base_name`: a plain name
    base (`class Foo(Bar):`) or a dotted-attribute base (`class Foo(pkg.Bar):`)
    both match on the base's simple (rightmost) name."""
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
    """The class-body statement directly inside `node` that assigns `name` a
    value — plain (`SCOPE = ...`) or annotated (`SCOPE: T = ...`). A bare
    annotation with no value (`SCOPE: T`) does not count: it declares a type
    but assigns nothing, so it would not satisfy the attribute at runtime
    either."""
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
    """The function or method named `name` defined directly inside `node`'s
    body, or None if the class body never defines it."""
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == name:
            return stmt
    return None


def find_numeric_get_defaults(tree: ast.Module) -> list[tuple[int, int]]:
    """(lineno, end_lineno) of each `x.get(key, <int/float literal>)` call.

    A silent numeric fallback: when `key` is missing this substitutes a made-up
    number instead of failing loud. `True`/`False` defaults are not numbers here.
    """
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
