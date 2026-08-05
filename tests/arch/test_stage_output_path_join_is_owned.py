"""A run dir is joined to a stage record's `output_path` in ONE place —
app.runtime.manifest.resolve_output_path. Two facts live there rather than at each
reader: the executor writes CSV where parquet cannot hold a frame, so the RECORDED
path (never one rebuilt from the stage id) says where the output landed; and a
recorded path escaping the run dir is refused rather than read.
"""
from __future__ import annotations

import ast
from pathlib import Path

_APP = Path(__file__).resolve().parents[2] / "app"
_OWNER = "app/runtime/manifest.py"
_RECORDED_PATH = "output_path"


def find_output_path_joins(tree: ast.AST, source: str) -> list[int]:
    """Line numbers of every `<anything> / <expr mentioning output_path>` division."""
    carriers = find_names_carrying_a_recorded_path(tree, source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        and _mentions_recorded_path(node.right, source, carriers)
    ]


def find_names_carrying_a_recorded_path(tree: ast.AST, source: str) -> set[str]:
    """Locals assigned from an expression that reads a record's `output_path`."""
    carriers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if _RECORDED_PATH not in (ast.get_source_segment(source, node.value) or ""):
            continue
        carriers.update(
            target.id for target in node.targets if isinstance(target, ast.Name)
        )
    return carriers


def _mentions_recorded_path(node: ast.expr, source: str, carriers: set[str]) -> bool:
    if isinstance(node, ast.Name):
        return node.id in carriers
    segment = ast.get_source_segment(source, node) or ""
    return _RECORDED_PATH in segment


def test_only_the_manifest_joins_a_run_dir_to_a_recorded_output_path() -> None:
    offenders: list[str] = []
    for path in sorted(_APP.rglob("*.py")):
        rel = path.relative_to(_APP.parent).as_posix()
        if rel == _OWNER:
            continue
        source = path.read_text(encoding="utf-8")
        offenders += [
            f"{rel}:{line}" for line in find_output_path_joins(ast.parse(source), source)
        ]
    assert not offenders, (
        "a run dir is joined to a recorded output_path outside "
        f"{_OWNER}::resolve_output_path — call that instead, so the CSV fallback and "
        "the escape check hold for every reader:\n  " + "\n  ".join(offenders)
    )


def test_the_detector_sees_the_shapes_a_reader_actually_writes() -> None:
    # Else the test above passes on a detector that matches nothing.
    for snippet in (
        "frame = run_dir / record.output_path\n",
        'frame = run_dir / stage_record["output_path"]\n',
        'rel = stage_record.get("output_path")\nframe = run_dir / rel\n',
    ):
        assert find_output_path_joins(ast.parse(snippet), snippet), snippet
    unrelated = "frame = run_dir / 'outputs' / f'{sid}.parquet'\n"
    assert not find_output_path_joins(ast.parse(unrelated), unrelated)
