"""Architecture: see #979 for why. `.as_py()` is owned like `test_frame_file_io_is_owned.py`."""
from __future__ import annotations

import ast
from pathlib import Path

from arch.test_complexity_ratchet import find_app_source_files

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

_OWNER = "app/core/frames.py"

# rel path -> why this file's .as_py() stays unwrapped. See #979.
_ALLOWLIST: dict[str, str] = {
    "app/runtime/citations.py": "_read_cell compares native values; see render_cell",
    "app/runtime/lineage.py": "sidecar decode into RowParent, re-cast by hand",
    "app/runtime/trace.py": "row converts once, whole, in trace_to_dict",
}

_CONVERTER = "convert_cell_to_json_value"


def find_unwrapped_as_py(tree: ast.AST) -> list[int]:
    parents = _build_parent_map(tree)
    offenders = []
    for node in ast.walk(tree):
        if not _is_as_py_call(node):
            continue
        if not _is_converter_call(_wrapping_call(node, parents)):
            offenders.append(node.lineno)
    return sorted(offenders)


def _wrapping_call(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.AST | None:
    current = parents.get(node)
    while isinstance(current, ast.IfExp):
        current = parents.get(current)
    return current


def _build_parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _is_as_py_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "as_py"
    )


def _is_converter_call(node: ast.AST | None) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == _CONVERTER
    if isinstance(func, ast.Attribute):
        return func.attr == _CONVERTER
    return False


def test_a_bare_as_py_cell_is_wrapped_or_explained() -> None:
    offenders: list[str] = []
    for path in find_app_source_files(_APP_ROOT):
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if rel == _OWNER or rel in _ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        offenders += [
            f"{rel}:{line}: .as_py() not wrapped in {_CONVERTER}(...) and the "
            f"file is not on the allowlist"
            for line in find_unwrapped_as_py(tree)
        ]
    assert not offenders, (
        "an Arrow cell left .as_py() without becoming a JsonScalar right there — "
        "wrap it in convert_cell_to_json_value(...), or add the file to "
        "_ALLOWLIST here with the reason it needs the native type (#979):\n  "
        + "\n  ".join(offenders)
    )


def test_the_allowlist_names_files_that_exist() -> None:
    missing = [rel for rel in _ALLOWLIST if not (_REPO_ROOT / rel).is_file()]
    assert not missing, f"stale allowlist entry: {missing}"


def test_a_wrapped_cell_is_not_an_offender() -> None:
    tree = ast.parse("x = convert_cell_to_json_value(col[i].as_py())\n")
    assert find_unwrapped_as_py(tree) == []


def test_a_wrapped_cell_through_an_attribute_import_is_not_an_offender() -> None:
    tree = ast.parse("x = frames.convert_cell_to_json_value(col[i].as_py())\n")
    assert find_unwrapped_as_py(tree) == []


def test_a_bare_cell_assigned_to_a_variable_is_an_offender() -> None:
    tree = ast.parse("cell = col[i].as_py()\nreturn convert_cell_to_json_value(cell)\n")
    assert find_unwrapped_as_py(tree) == [1]


def test_a_bare_cell_in_an_fstring_is_an_offender() -> None:
    tree = ast.parse('label = f"{col[i].as_py()!r}"\n')
    assert find_unwrapped_as_py(tree) == [1]


def test_a_bare_cell_returned_directly_is_an_offender() -> None:
    tree = ast.parse("def f():\n    return col[i].as_py()\n")
    assert find_unwrapped_as_py(tree) == [2]


def test_a_cell_wrapped_through_a_ternary_is_not_an_offender() -> None:
    tree = ast.parse(
        "x = convert_cell_to_json_value(v.as_py() if hasattr(v, 'as_py') else v)\n"
    )
    assert find_unwrapped_as_py(tree) == []


def test_a_cell_deferred_to_a_named_step_is_still_an_offender() -> None:
    tree = ast.parse("cell = col[i].as_py()\nx = convert_cell_to_json_value(cell)\n")
    assert find_unwrapped_as_py(tree) == [1]
