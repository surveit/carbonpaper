"""Architecture: every internal column/document key the machinery spends stays under
`INTERNAL_COLUMN_PREFIX` — the namespace a stage is forbidden to declare (see
app.models.stages.shared), which is what buys collision-freedom for keys inside it
and for nothing else. Read by convention: a module-level constant named `*_KEY(S)`
or containing INTERNAL, its string values resolved through same-module references.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

from app.models.stages.shared import INTERNAL_COLUMN_PREFIX

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# What makes a constant an internal-key declaration to this rule. INTERNAL is
# matched as well as the `_KEY(S)` suffix so the ONE declaration table of the
# row driver's internal columns (`_INTERNAL_ROW_COLUMNS`) is covered — it names
# its members by reference, which is why values are resolved below rather than
# read as literals.
_KEY_CONSTANT_SUFFIXES = ("_KEY", "_KEYS")
_INTERNAL_MARKER = "INTERNAL"


def is_internal_key_name(name: str) -> bool:
    return name.endswith(_KEY_CONSTANT_SUFFIXES) or _INTERNAL_MARKER in name


def find_module_string_constants(tree: ast.Module) -> dict[str, list[str]]:
    """Every module-level constant that holds string literals, name -> values.
    The resolution table for references: `_INTERNAL_ROW_COLUMNS` lists
    `ROW_ERROR_KEY`, not "_error"."""
    constants: dict[str, list[str]] = {}
    for stmt in tree.body:
        name = _assigned_constant_name(stmt)
        if name is None:
            continue
        values, unresolved = _string_values(stmt.value, {})
        if values and not unresolved:
            constants[name] = values
    return constants


def find_key_constants(tree: ast.Module) -> list[tuple[int, str, list[str], list[str]]]:
    """(lineno, name, values, unresolved) per module-level constant this rule
    reads, with `values` the string literals it holds — following same-module
    references — and `unresolved` the names it could not follow."""
    constants = find_module_string_constants(tree)
    found: list[tuple[int, str, list[str], list[str]]] = []
    for stmt in tree.body:
        name = _assigned_constant_name(stmt)
        if name is None or not is_internal_key_name(name):
            continue
        values, unresolved = _string_values(stmt.value, constants)
        found.append((stmt.lineno, name, values, unresolved))
    return found


def _assigned_constant_name(stmt: ast.stmt) -> str | None:
    """The single name a module-level statement assigns — plain (`X = ...`) or
    annotated (`X: T = ...`) — or None if it assigns no single plain name."""
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return stmt.target.id if isinstance(stmt.target, ast.Name) else None
    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        return stmt.targets[0].id if isinstance(stmt.targets[0], ast.Name) else None
    return None


def _string_values(
    value: ast.expr | None, constants: dict[str, list[str]]
) -> tuple[list[str], list[str]]:
    """The string literals anywhere in `value` (bare, in a collection, or passed
    to a constructor), plus the names that could not be resolved through
    `constants`. A name in the callee position is a constructor, not a key, so it
    counts as neither. Non-string literals (a bool flag on a table row) are
    ignored — they are not names, so they hide nothing."""
    if value is None:
        return ([], [])
    callees = {
        node.func.id
        for node in ast.walk(value)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    values: list[str] = []
    unresolved: list[str] = []
    for node in ast.walk(value):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            values.append(node.value)
        elif isinstance(node, ast.Name) and node.id not in callees:
            resolved = constants.get(node.id)
            if resolved is None:
                unresolved.append(node.id)
            else:
                values.extend(resolved)
    return (values, unresolved)


def find_unprefixed_internal_keys(paths: list[Path], repo_root: Path) -> list[str]:
    """One "<path>:<lineno>  <NAME> …" per internal-key constant under `paths`
    the prefix ban does not cover: a value outside the reserved namespace, or a
    reference this rule cannot follow (an unverifiable key is a hole, not a
    pass)."""
    offenders: list[str] = []
    for path in paths:
        location = path.relative_to(repo_root).as_posix()
        for lineno, name, values, unresolved in find_key_constants(parse_module(path)):
            offenders.extend(
                f"{location}:{lineno}  {name} = {value!r} — outside the reserved namespace"
                for value in values
                if not value.startswith(INTERNAL_COLUMN_PREFIX)
            )
            offenders.extend(
                f"{location}:{lineno}  {name} references {ref} — this rule cannot "
                "resolve it, so the value is unverifiable"
                for ref in unresolved
            )
    return offenders


def test_every_internal_key_sits_under_the_reserved_prefix() -> None:
    offenders = find_unprefixed_internal_keys(find_source_files_under(_APP_ROOT), _REPO_ROOT)
    assert not offenders, (
        f"an internal column/document key must be named with the "
        f"`{INTERNAL_COLUMN_PREFIX}` prefix a stage is forbidden to declare — a key "
        "outside it can collide with a real column the compiler authored, and the "
        "stage-side ban buys it nothing. Rename the value (not the constant), or, if "
        "this constant is not an internal key at all, take INTERNAL / the _KEY(S) "
        "suffix out of its name so this rule stops reading it:\n  " + "\n  ".join(offenders)
    )


def test_the_scan_finds_at_least_one_internal_key() -> None:
    """A rule that discovers nothing passes vacuously. Asserted on the COUNT, not
    on specific constant names, so renaming a key does not fail this — only the
    convention drifting out from under it does."""
    found = [
        name
        for path in find_source_files_under(_APP_ROOT)
        for _, name, _, _ in find_key_constants(parse_module(path))
    ]
    assert found, (
        "no constant under app/ matches the naming convention this rule reads "
        f"(`*{_KEY_CONSTANT_SUFFIXES[0]}(S)` or containing {_INTERNAL_MARKER}) — the "
        "convention no longer matches how internal keys are declared, so the rule is "
        "guarding an empty set. Update the convention to match the code."
    )


# --- unit tests for the checker, on inline snippets (red + green) ---------


def test_find_key_constants_reads_a_plain_string_constant() -> None:
    tree = ast.parse('ROW_ERROR_KEY = "_error"\n')
    assert find_key_constants(tree) == [(1, "ROW_ERROR_KEY", ["_error"], [])]


def test_find_key_constants_reads_an_annotated_set_constant() -> None:
    tree = ast.parse('IGNORE_KEYS: set[str] = {"_filename"}\n')
    assert find_key_constants(tree) == [(1, "IGNORE_KEYS", ["_filename"], [])]


def test_find_key_constants_reads_a_frozenset_call_without_flagging_the_callee() -> None:
    tree = ast.parse('IGNORE_KEYS = frozenset({"_a", "_b"})\n')
    (_, _, values, unresolved) = find_key_constants(tree)[0]
    assert sorted(values) == ["_a", "_b"]
    assert unresolved == []


def test_find_key_constants_matches_a_name_containing_internal() -> None:
    """The declaration-table shape: matched on INTERNAL, not on a _KEY suffix."""
    tree = ast.parse('_INTERNAL_ROW_COLUMNS = ("_error",)\n')
    assert find_key_constants(tree) == [(1, "_INTERNAL_ROW_COLUMNS", ["_error"], [])]


def test_find_key_constants_resolves_a_reference_to_another_constant() -> None:
    """What matching the table costs: its members are names, so the rule traces
    them to the literals they hold."""
    tree = ast.parse(
        'ROW_ERROR_KEY = "_error"\n'
        "_INTERNAL_ROW_COLUMNS = (Row(ROW_ERROR_KEY, stripped=True),)\n"
    )
    table = [entry for entry in find_key_constants(tree) if entry[1].startswith("_INTERNAL")]
    assert table == [(2, "_INTERNAL_ROW_COLUMNS", ["_error"], [])]


def test_find_key_constants_reports_a_reference_it_cannot_follow() -> None:
    tree = ast.parse("_INTERNAL_ROW_COLUMNS = (Row(IMPORTED_KEY),)\n")
    assert find_key_constants(tree) == [(1, "_INTERNAL_ROW_COLUMNS", [], ["IMPORTED_KEY"])]


def test_find_key_constants_ignores_a_name_without_the_suffix_or_marker() -> None:
    tree = ast.parse('ROW_ERROR = "error"\n')
    assert find_key_constants(tree) == []


def test_find_key_constants_ignores_a_nested_assignment() -> None:
    """Module level only — a name bound inside a function is not a declared key."""
    tree = ast.parse('def f():\n    ROW_ERROR_KEY = "error"\n')
    assert find_key_constants(tree) == []


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


def test_find_unprefixed_internal_keys_flags_an_unresolvable_reference(tmp_path: Path) -> None:
    target = tmp_path / "computed.py"
    target.write_text("ROW_ERROR_KEY = build_key(SOME_OTHER)\n")
    (offender,) = find_unprefixed_internal_keys([target], tmp_path)
    assert "cannot resolve" in offender


def test_find_unprefixed_internal_keys_flags_a_key_reached_through_a_reference(
    tmp_path: Path,
) -> None:
    """Resolution is not a way out: a bad literal is caught through the reference
    as well as at the declaration."""
    target = tmp_path / "traced.py"
    target.write_text('ROW_ERROR_KEY = "error"\n_INTERNAL_ROWS = (Row(ROW_ERROR_KEY),)\n')
    offenders = find_unprefixed_internal_keys([target], tmp_path)
    assert len(offenders) == 2
    assert all("'error'" in offender for offender in offenders)
