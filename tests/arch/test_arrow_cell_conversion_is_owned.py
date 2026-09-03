"""Architecture: see #979/#985 for why. `.as_py()` is owned like `test_frame_file_io_is_owned.py`."""
from __future__ import annotations

import ast
from pathlib import Path

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

_ALLOWLIST: frozenset[str] = frozenset({"app/core/frames.py"})


def find_as_py_calls(tree: ast.AST) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "as_py"
    )


def test_as_py_is_called_only_from_frames() -> None:
    offenders: list[str] = []
    for path in find_app_source_files(_APP_ROOT):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [f"{rel}:{line}: .as_py()" for line in find_as_py_calls(tree)]
    assert not offenders, (
        "an Arrow cell read outside app/core/frames.py — use read_native_cell / "
        "read_native_row / read_native_column / read_native_scalar there instead "
        "(#979/#985):\n  " + "\n  ".join(offenders)
    )


def test_the_allowlist_names_files_that_exist() -> None:
    missing = [rel for rel in _ALLOWLIST if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"stale allowlist entry: {missing}"


def test_the_detector_sees_the_shape_a_caller_actually_writes() -> None:
    for snippet in (
        "x = col[i].as_py()\n",
        "x = frame.column(name)[i].as_py()\n",
        "x = convert_cell_to_json_value(col[i].as_py())\n",
    ):
        assert find_as_py_calls(ast.parse(snippet)), snippet
    for allowed in (
        "x = read_native_cell(table, name, i)\n",
        "x = convert_cell_to_json_value(read_native_cell(table, name, i))\n",
    ):
        assert not find_as_py_calls(ast.parse(allowed)), allowed
