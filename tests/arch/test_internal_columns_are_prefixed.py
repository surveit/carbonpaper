"""Architecture: every internal column/document key the machinery spends must sit
under `INTERNAL_COLUMN_PREFIX`. That prefix is what a stage is FORBIDDEN to
declare (app.models.stages.shared.find_internal_namespace_column_issues, enforced
by Stage._schemas_declared) — so collision-freedom is bought for keys inside it
and for nothing else. Discovery is by convention: a module-level `*_KEY`/`*_KEYS`.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

from app.models.stages.shared import INTERNAL_COLUMN_PREFIX

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"
_KEY_CONSTANT_SUFFIXES = ("_KEY", "_KEYS")


def find_key_constants(tree: ast.Module) -> list[tuple[int, str, list[str] | None]]:
    """(lineno, name, values) for each module-level constant whose name marks it
    an internal key by convention — public and `*_KEY`/`*_KEYS`. `values` is the
    string literals it holds; None when the value is not a string literal or a
    literal collection of them, which no checker can read and the rule treats as
    a violation of its own (see find_unprefixed_internal_keys)."""
    found: list[tuple[int, str, list[str] | None]] = []
    for stmt in tree.body:
        name = _assigned_constant_name(stmt)
        if name is None or name.startswith("_") or not name.endswith(_KEY_CONSTANT_SUFFIXES):
            continue
        assert isinstance(stmt, (ast.Assign, ast.AnnAssign))  # _assigned_constant_name
        found.append((stmt.lineno, name, _string_literals(stmt.value)))
    return found


def _assigned_constant_name(stmt: ast.stmt) -> str | None:
    """The single name a module-level statement assigns — plain (`X = ...`) or
    annotated (`X: T = ...`) — or None if it assigns no single plain name (a
    bare annotation, a tuple target, anything else)."""
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return stmt.target.id if isinstance(stmt.target, ast.Name) else None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        return stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else None
    return None


def _string_literals(value: ast.expr | None) -> list[str] | None:
    """The string literals `value` holds: a bare `"_x"`, a set/list/tuple literal
    of them, or `frozenset({...})`/`set({...})` around one. None when `value` is
    anything else — a name, a comprehension, a computed expression."""
    if isinstance(value, ast.Constant):
        return [value.value] if isinstance(value.value, str) else None
    if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in value.elts):
            return [e.value for e in value.elts]  # type: ignore[attr-defined]
        return None
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in ("frozenset", "set")
        and len(value.args) == 1
    ):
        return _string_literals(value.args[0])
    return None


def find_unprefixed_internal_keys(paths: list[Path], repo_root: Path) -> list[str]:
    """One "<path>:<lineno>  <NAME> = <what's wrong>" per internal-key constant
    under `paths` that the prefix ban does not cover: a literal outside the
    reserved namespace, or a value no checker can read (an unreadable one is
    reported too — an unverifiable key is a hole, not a pass)."""
    offenders: list[str] = []
    for path in paths:
        location = path.relative_to(repo_root).as_posix()
        for lineno, name, values in find_key_constants(parse_module(path)):
            if values is None:
                offenders.append(
                    f"{location}:{lineno}  {name} — value is not a string literal, "
                    "so this rule cannot verify it"
                )
                continue
            offenders.extend(
                f"{location}:{lineno}  {name} = {value!r} — outside the reserved namespace"
                for value in values
                if not value.startswith(INTERNAL_COLUMN_PREFIX)
            )
    return offenders


def test_every_internal_key_sits_under_the_reserved_prefix() -> None:
    offenders = find_unprefixed_internal_keys(find_source_files_under(_APP_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"an internal column/document key must be named with the "
        f"`{INTERNAL_COLUMN_PREFIX}` prefix a stage is forbidden to declare — a key "
        "outside it can collide with a real column the compiler authored, and the "
        "stage-side ban buys it nothing. Rename the value (not the constant), or, if "
        "this is not an internal key at all, drop the _KEY/_KEYS suffix that makes "
        "this rule read it:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_finds_the_keys_the_machinery_actually_spends() -> None:
    """A rule that discovers nothing passes vacuously. The row driver's internal
    columns and row lineage must both be FOUND by the naming convention — if they
    stop being, the convention no longer matches how keys are declared and this
    file is guarding an empty set."""
    found = {
        name
        for path in find_source_files_under(_APP_ROOT)
        for _, name, _ in find_key_constants(parse_module(path))
    }
    assert {
        "ROW_ERROR_KEY", "ROW_USAGE_KEY", "ROW_DEFERRED_KEY",
        "TRACE_SOURCE_STAGE_KEY", "TRACE_SOURCE_ROW_KEY",
    } <= found


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_key_constants_reads_a_plain_string_constant() -> None:
    tree = ast.parse('ROW_ERROR_KEY = "_error"\n')
    assert find_key_constants(tree) == [(1, "ROW_ERROR_KEY", ["_error"])]


def test_find_key_constants_reads_an_annotated_set_constant() -> None:
    tree = ast.parse('IGNORE_KEYS: set[str] = {"_filename"}\n')
    assert find_key_constants(tree) == [(1, "IGNORE_KEYS", ["_filename"])]


def test_find_key_constants_reads_a_frozenset_call() -> None:
    tree = ast.parse('IGNORE_KEYS = frozenset({"_a", "_b"})\n')
    (_, _, values) = find_key_constants(tree)[0]
    assert sorted(values or []) == ["_a", "_b"]


def test_find_key_constants_ignores_a_private_constant() -> None:
    """A module-private constant is not a shared key the machinery spends."""
    tree = ast.parse('_LOCAL_KEY = "whatever"\n')
    assert find_key_constants(tree) == []


def test_find_key_constants_ignores_a_name_without_the_suffix() -> None:
    tree = ast.parse('ROW_ERROR = "error"\n')
    assert find_key_constants(tree) == []


def test_find_key_constants_ignores_a_nested_assignment() -> None:
    """Module level only — a name bound inside a function or class is not a
    declared key."""
    tree = ast.parse('def f():\n    ROW_ERROR_KEY = "error"\n')
    assert find_key_constants(tree) == []


def test_find_key_constants_reports_an_unreadable_value_as_none() -> None:
    tree = ast.parse("ROW_ERROR_KEY = build_key()\n")
    assert find_key_constants(tree) == [(1, "ROW_ERROR_KEY", None)]


def test_find_unprefixed_internal_keys_passes_a_prefixed_key(tmp_path: Path) -> None:
    target = tmp_path / "clean.py"
    target.write_text('ROW_ERROR_KEY = "_error"\n')
    assert find_unprefixed_internal_keys([target], tmp_path) == []


def test_find_unprefixed_internal_keys_flags_an_unprefixed_key(tmp_path: Path) -> None:
    target = tmp_path / "dirty.py"
    target.write_text('ROW_ERROR_KEY = "error"\n')
    (offender,) = find_unprefixed_internal_keys([target], tmp_path)
    assert offender.startswith("dirty.py:1  ROW_ERROR_KEY = 'error'")


def test_find_unprefixed_internal_keys_flags_one_bad_member_of_a_set(tmp_path: Path) -> None:
    target = tmp_path / "mixed.py"
    target.write_text('IGNORE_KEYS = {"_filename", "order"}\n')
    (offender,) = find_unprefixed_internal_keys([target], tmp_path)
    assert "'order'" in offender


def test_find_unprefixed_internal_keys_flags_an_unverifiable_key(tmp_path: Path) -> None:
    target = tmp_path / "computed.py"
    target.write_text("ROW_ERROR_KEY = build_key()\n")
    (offender,) = find_unprefixed_internal_keys([target], tmp_path)
    assert "cannot verify" in offender
