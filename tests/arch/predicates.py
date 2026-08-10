"""Reusable structural checks for architecture tests.

Each predicate takes the files to inspect and returns a list of human-readable
offender strings (empty when the rule holds). Predicates read source as AST and
never import the modules they inspect.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from arch._helpers import (
    collect_called_funcs,
    collect_called_methods,
    find_dict_key_uses,
    find_function_defs,
    find_imported_modules,
    find_numeric_get_defaults,
    parse_module,
)

_PRODUCTION_RUN_MODULE = "app.runtime.runner"

_DISK_BUILTINS = {"open"}
_DISK_METHODS = {
    "open",
    "read_text",
    "write_text",
    "read_bytes",
    "write_bytes",
    "unlink",
    "mkdir",
    "rmdir",
    "touch",
}
_DATA_DEFAULT_OK = "# data-default-ok"


def check_no_raw_disk(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        hits = (collect_called_funcs(tree) & _DISK_BUILTINS) | (
            collect_called_methods(tree) & _DISK_METHODS
        )
        if hits:
            offenders.append(f"{path.name}: {sorted(hits)}")
    return offenders


def check_no_import(paths: list[Path], module: str, *, allow: set[str]) -> list[str]:
    allowed = {suffix.replace("\\", "/") for suffix in allow}
    offenders: list[str] = []
    for path in paths:
        posix = path.as_posix()
        if any(posix.endswith(suffix) for suffix in allowed):
            continue
        imported = find_imported_modules(parse_module(path))
        if any(name == module or name.startswith(f"{module}.") for name in imported):
            offenders.append(posix)
    return offenders


def check_imports_are_stdlib_only(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        for node in ast.walk(parse_module(path)):
            if isinstance(node, ast.ImportFrom):
                if node.level:  # relative: `from . / .. import ...`
                    offenders.append(f"{path.name}:{node.lineno}  relative import")
                elif node.module and node.module.split(".")[0] not in sys.stdlib_module_names:
                    offenders.append(f"{path.name}:{node.lineno}  from {node.module} import ...")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] not in sys.stdlib_module_names:
                        offenders.append(f"{path.name}:{node.lineno}  import {alias.name}")
    return offenders


def check_no_dict_keys(paths: list[Path], keys: set[str]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        for lineno, key in find_dict_key_uses(parse_module(path), keys):
            offenders.append(f"{path.name}:{lineno}  key {key!r}")
    return offenders


def check_no_fabricated_numbers(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for start, end in find_numeric_get_defaults(parse_module(path)):
            if not any(_DATA_DEFAULT_OK in line for line in lines[start - 1 : end]):
                offenders.append(f"{path}:{start}  {lines[start - 1].strip()}")
    return offenders


def find_production_run_imports(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        imported = find_imported_modules(parse_module(path))
        if any(
            name == _PRODUCTION_RUN_MODULE
            or name.startswith(f"{_PRODUCTION_RUN_MODULE}.")
            for name in imported
        ):
            offenders.append(path.as_posix())
    return offenders


_PROJECT_DIR_NAMES = {"project_dir", "pdir", "project_directory"}
_PROJECTS_ROOT_FUNC = "projects_dir"


def find_project_directory_names(paths: list[Path], *, root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        rel = path.relative_to(root).as_posix()
        for node in ast.walk(parse_module(path)):
            line = getattr(node, "lineno", 0)
            offenders += [f"{rel}:{line} {detail}" for detail in _describe_node(node)]
    return sorted(offenders)


def _describe_node(node: ast.AST) -> list[str]:
    if isinstance(node, ast.arg) and node.arg in _PROJECT_DIR_NAMES:
        return [f"parameter `{node.arg}` — take a project id (str) instead"]
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        if node.id in _PROJECT_DIR_NAMES:
            return [f"binds `{node.id}` — resolve the directory inside a service instead"]
    if _is_projects_root_join(node):
        return [f"joins onto `{_PROJECTS_ROOT_FUNC}()` — call a service that takes the id"]
    return []


def _is_projects_root_join(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == _PROJECTS_ROOT_FUNC
    )


def find_check_prefixed_functions(paths: list[Path]) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        for name, lineno in find_function_defs(parse_module(path)):
            if name.startswith(("check_", "_check_")):
                offenders.append(f"{path}:{lineno}  def {name}")
    return offenders


def find_banned_words(paths: list[Path], banned: set[str], exempt: set[Path]) -> list[str]:
    resolved_exempt = {path.resolve() for path in exempt}
    offenders: list[str] = []
    for path in paths:
        if path.resolve() in resolved_exempt:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            lowered = line.lower()
            hits = sorted(word for word in banned if word in lowered)
            if hits:
                offenders.append(f"{path}:{lineno}  [{', '.join(hits)}]  {line.strip()}")
    return offenders
