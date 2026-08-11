"""Default-deny import rule: inside named package roots, a file may import only
the dotted module names on its allowlist. Unlike a forbidden-module list, a new
banned module needs no edit here — anything unlisted is already denied.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module


def find_disallowed_imports(
    paths: list[Path], *, roots: set[str], allow: set[str]
) -> list[str]:
    if not paths:
        raise ValueError(
            "find_disallowed_imports was handed no files — a rule that governs "
            "zero files is a silent pass; check the caller's path filter (the "
            "governed file was probably renamed or moved)"
        )
    offenders: list[str] = []
    for path in paths:
        for lineno, statement, imported in _find_governed_imports(parse_module(path), roots):
            if not _is_allowed(imported, allow):
                offenders.append(f"{path.as_posix()}:{lineno}  {statement}")
    return offenders


def _find_governed_imports(
    tree: ast.Module, roots: set[str]
) -> list[tuple[int, str, str | None]]:
    found: list[tuple[int, str, str | None]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [
                (node.lineno, f"import {alias.name}", alias.name)
                for alias in node.names
                if _root_of(alias.name) in roots
            ]
        elif isinstance(node, ast.ImportFrom):
            found += _find_from_imports(node, roots)
    return found


def _find_from_imports(
    node: ast.ImportFrom, roots: set[str]
) -> list[tuple[int, str, str | None]]:
    if node.level:
        dots = "." * node.level
        return [
            (node.lineno, f"from {dots}{node.module or ''} import {alias.name}", None)
            for alias in node.names
        ]
    if node.module is None or _root_of(node.module) not in roots:
        return []
    return [
        (
            node.lineno,
            f"from {node.module} import {alias.name}",
            f"{node.module}.{alias.name}",
        )
        for alias in node.names
    ]


def _is_allowed(imported: str | None, allow: set[str]) -> bool:
    if imported is None:
        return False
    # `from pkg import name` is tested as pkg.name first, so an allowlist can
    # permit one submodule of an otherwise denied package; falling back to the
    # package covers `from pkg import SOME_CONSTANT`.
    package = imported.rsplit(".", 1)[0]
    return any(_covers(entry, imported) or _covers(entry, package) for entry in allow)


def _covers(entry: str, name: str) -> bool:
    return name == entry or name.startswith(f"{entry}.")


def _root_of(dotted: str) -> str:
    return dotted.split(".", 1)[0]
