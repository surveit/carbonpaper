"""A run's kind stays a `RunKind`. docs/run-manifest.md"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import scan_all_text

_REPO_ROOT = Path(__file__).resolve().parents[2]

_RESOLVER = "resolve_kind_dir"
_RESOLVER_MODULE = "app/services/workspace.py"

_KIND_TYPE = "RunKind"
_KIND_NAMES = {"kind", "expected_kind"}


def find_kind_value_unwrappings(tree: ast.Module) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "value"
        and _reads_a_run_kind(node.value)
    )


def find_stringly_typed_kinds(tree: ast.Module) -> list[int]:
    return sorted(
        arg.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for arg in _every_arg(node.args)
        if arg.arg in _KIND_NAMES and _annotates_a_bare_str(arg.annotation)
    )


def find_kind_offenders(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        relative = path.relative_to(repo_root).as_posix()
        if path == Path(__file__):
            continue
        tree = parse_module(path)
        allowed = _find_resolver_lines(tree) if relative == _RESOLVER_MODULE else set()
        offenders += [f"{relative}:{n}" for n in find_kind_value_unwrappings(tree)
                      if n not in allowed]
        # `kind` names an event's and a branch's too. docs/run-manifest.md
        if _imports_the_kind_type(tree):
            offenders += [f"{relative}:{n}" for n in find_stringly_typed_kinds(tree)]
    return sorted(offenders)


def _imports_the_kind_type(tree: ast.Module) -> bool:
    return any(alias.name == _KIND_TYPE
               for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
               for alias in node.names)


def _every_arg(args: ast.arguments) -> list[ast.arg]:
    return [*args.posonlyargs, *args.args, *args.kwonlyargs]


def _annotates_a_bare_str(annotation: ast.expr | None) -> bool:
    return isinstance(annotation, ast.Name) and annotation.id == "str"


def _reads_a_run_kind(node: ast.expr) -> bool:
    return (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == _KIND_TYPE)


def _find_resolver_lines(tree: ast.Module) -> set[int]:
    found = next((n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == _RESOLVER), None)
    if found is None:
        raise AssertionError(
            f"{_RESOLVER} is gone from {_RESOLVER_MODULE}; this test guards it by name")
    return set(range(found.lineno, (found.end_lineno or found.lineno) + 1))


def test_a_run_kind_is_never_unwrapped_or_widened_to_a_string() -> None:
    offenders = find_kind_offenders(scan_all_text((".py",)), _REPO_ROOT)
    assert not offenders, (
        f"a run's kind stopped being a {_KIND_TYPE} here — pass the member and let "
        f"{_RESOLVER} spell it:\n  " + "\n  ".join(offenders)
    )
