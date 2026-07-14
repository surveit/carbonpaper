"""Reusable structural checks for architecture tests.

Each predicate takes the files to inspect and returns a list of human-readable
offender strings (empty when the rule holds). Predicates read source as AST and
never import the modules they inspect.
"""
from __future__ import annotations

from pathlib import Path

from arch._helpers import (
    collect_called_funcs,
    collect_called_methods,
    collect_string_literals,
    find_imported_modules,
    find_numeric_get_defaults,
    parse_module,
)

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
    """Files that call filesystem builtins/methods directly (open, write_text, …)."""
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
    """Files that import `module` (or a submodule of it), except those whose path
    ends with an entry in `allow`. Seals a backend behind its one owner — e.g.
    `sqlite3` may be imported only by app/core/persistence.py, so no subsystem
    talks to the database directly."""
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


def check_no_fabricated_numbers(paths: list[Path]) -> list[str]:
    """Files with a silent numeric ``.get(k, <int/float>)`` fallback (unless opted out)."""
    offenders: list[str] = []
    for path in paths:
        lines = path.read_text(encoding="utf-8").splitlines()
        for start, end in find_numeric_get_defaults(parse_module(path)):
            if not any(_DATA_DEFAULT_OK in line for line in lines[start - 1 : end]):
                offenders.append(f"{path}:{start}  {lines[start - 1].strip()}")
    return offenders


def check_literal_confined(
    paths: list[Path], *, literal: str, owner_suffix: str
) -> list[str]:
    """Files other than the owner whose CODE contains the exact string `literal`.

    Confines a directory-name literal to the one module that owns that directory
    (sole-writer). Matches whole string constants only — building a path from the
    name trips it; prose or a longer label containing the word does not."""
    offenders: list[str] = []
    for path in paths:
        if path.as_posix().endswith(owner_suffix):
            continue
        if literal in collect_string_literals(parse_module(path)):
            offenders.append(f"{path}: string literal {literal!r}")
    return offenders
