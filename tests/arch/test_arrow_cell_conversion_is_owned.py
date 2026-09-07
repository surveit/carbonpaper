"""Architecture: see #979/#985 for why. Two chokepoints, owned like `test_frame_file_io_is_owned.py`."""
from __future__ import annotations

import ast
from pathlib import Path

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

_OWNER = "app/core/frames.py"

# rel path -> raw reads it may call. See #979/#985 for why each one is here.
_RAW_READERS: dict[str, str] = {
    "app/runtime/citations.py": "read_native_cell",
    "app/runtime/lineage.py": "read_native_cell,read_native_column",
    "app/runtime/trace.py": "read_native_row",
    "app/runtime/branch_analysis/branch_cache.py": "read_native_cell",
}

_RAW_NAMES = frozenset(
    {"read_native_scalar", "read_native_cell", "read_native_row", "read_native_column"}
)


def find_as_py_calls(tree: ast.AST) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "as_py"
    )


def find_raw_reader_calls(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _called_name(node.func)
        if name in _RAW_NAMES:
            found.append((node.lineno, name))
    return sorted(found)


def _called_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def test_as_py_is_called_only_from_frames() -> None:
    offenders: list[str] = []
    for path in find_app_source_files(_APP_ROOT):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{rel}:{line}: .as_py()" for line in find_as_py_calls(tree)]
    assert not offenders, (
        "an Arrow cell read outside app/core/frames.py — use a read_native_* "
        "function there instead (#979/#985):\n  " + "\n  ".join(offenders)
    )


def test_raw_native_reads_are_called_only_where_named() -> None:
    offenders: list[str] = []
    for path in find_app_source_files(_APP_ROOT):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _OWNER:
            continue
        allowed = set(_RAW_READERS.get(rel, "").split(",")) if rel in _RAW_READERS else set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{rel}:{line}: {name}(...) — use {name}_as_json, or name this file "
            f"in _RAW_READERS with why it needs the native value"
            for line, name in find_raw_reader_calls(tree)
            if name not in allowed
        ]
    assert not offenders, (
        "a raw (non-JSON) native-value read outside its named owner (#979/#985):\n  "
        + "\n  ".join(offenders)
    )


def test_the_allowlist_names_files_that_exist() -> None:
    missing = [rel for rel in _RAW_READERS if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"stale allowlist entry: {missing}"


def test_the_detector_sees_the_shape_a_caller_actually_writes() -> None:
    for snippet in (
        "x = col[i].as_py()\n",
        "x = frame.column(name)[i].as_py()\n",
    ):
        assert find_as_py_calls(ast.parse(snippet)), snippet
    assert not find_as_py_calls(ast.parse("x = read_native_cell(table, name, i)\n"))
    for name in ("read_native_scalar", "read_native_cell", "read_native_row", "read_native_column"):
        assert find_raw_reader_calls(ast.parse(f"x = {name}(a, b)\n")) == [(1, name)]
    assert not find_raw_reader_calls(ast.parse("x = read_native_cell_as_json(t, n, i)\n"))
