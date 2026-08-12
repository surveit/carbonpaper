"""A TableRef's `path` is joined to a root in ONE place — app.evals.dataset.read_table_ref,
which resolves the root from the project id. Everywhere else the root is an argument
someone chose, and the checkout is always in reach: that is how the eval dataset came to
be read out of app/seeds/data instead of out of the project that owns it.
"""
from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_OWNER = "app/evals/dataset.py"
_TABLE_PATH = "table.path"


def find_table_path_joins(tree: ast.AST, source: str) -> list[int]:
    carriers = find_names_carrying_a_table_path(tree, source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        and _mentions_a_table_path(node.right, source, carriers)
    ]


def find_names_carrying_a_table_path(tree: ast.AST, source: str) -> set[str]:
    carriers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if _TABLE_PATH not in (ast.get_source_segment(source, node.value) or ""):
            continue
        carriers.update(target.id for target in node.targets if isinstance(target, ast.Name))
    return carriers


def _mentions_a_table_path(node: ast.expr, source: str, carriers: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in carriers
    return _TABLE_PATH in (ast.get_source_segment(source, node) or "")


def test_only_the_dataset_reader_joins_a_root_to_a_table_path() -> None:
    offenders: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP.parent).as_posix()
        if rel == _OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        offenders += [
            f"{rel}:{line}" for line in find_table_path_joins(ast.parse(source), source)
        ]
    assert not offenders, (
        f"a root is joined to a TableRef path outside {_OWNER}::read_table_ref — call that "
        "instead, so every reader resolves the dataset against the project that owns it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_detector_sees_the_shapes_that_shipped() -> None:
    for snippet in (
        "frame = read_frame_file(REPO_ROOT / config.table.path)\n",   # app/web/routers/evals.py
        "path = repo_root / table.path\n",                            # app/evals/dataset.py
        "rel = config.table.path\nframe = read_frame_file(root / rel)\n",
    ):
        assert find_table_path_joins(ast.parse(snippet), snippet), snippet
    unrelated = "frame = run_dir / 'outputs' / f'{sid}.parquet'\n"
    assert not find_table_path_joins(ast.parse(unrelated), unrelated)
