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
    find_dict_key_uses,
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


def check_no_call(paths: list[Path], names: set[str]) -> list[str]:
    """Files that call a bare function or method named in `names` anywhere —
    e.g. `check_no_call(paths, {"copytree"})` catches both `copytree(...)`
    (after `from shutil import copytree`) and `shutil.copytree(...)`; the rule
    is about the operation, not which module it was imported from. A
    generalisation of `check_no_raw_disk`'s fixed builtin/method sets to an
    arbitrary caller-supplied name set."""
    offenders: list[str] = []
    for path in paths:
        tree = parse_module(path)
        hits = (collect_called_funcs(tree) | collect_called_methods(tree)) & names
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


def check_no_dict_keys(paths: list[Path], keys: set[str]) -> list[str]:
    """Files that read or write any of `keys` as a dict key (subscript, .get,
    or dict literal). Keeps domain vocabulary out of a module that must stay
    generic — e.g. the runner must not touch connector params like "path"; only
    the owning stage module may."""
    offenders: list[str] = []
    for path in paths:
        for lineno, key in find_dict_key_uses(parse_module(path), keys):
            offenders.append(f"{path.name}:{lineno}  key {key!r}")
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
