"""Architecture: a ``WorkflowStage`` is built twice, for two different reasons.
``app/models/workflow.py`` resolves it from the whole graph — the normal path, where a
stage's input schemas are whatever its upstreams actually emit. ``app/runtime/stage_tests.py``
builds it from the stage's own signature instead, because an authored stage test states
what the stage does given exactly what it declares reading, not what a graph happens to hand it.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = _REPO_ROOT / "app"
_NAME = "WorkflowStage"

_BUILDERS = (
    "app/models/workflow.py",       # from the graph: upstream outputs are the inputs
    "app/runtime/stage_tests.py",   # from the signature: the stage's own declaration
)


def find_workflow_stage_constructions(tree: ast.Module) -> list[int]:
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _constructs_workflow_stage(node.func)
    )


def find_workflow_stage_builders(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        module_path = path.relative_to(repo_root).as_posix()
        if module_path in _BUILDERS:
            continue
        offenders += [
            f"{module_path}:{lineno}"
            for lineno in find_workflow_stage_constructions(parse_module(path))
        ]
    return offenders


def _constructs_workflow_stage(func: ast.expr) -> bool:
    if isinstance(func, ast.Name):
        return func.id == _NAME
    return isinstance(func, ast.Attribute) and func.attr == _NAME


def test_workflow_stage_is_built_in_two_places() -> None:
    offenders = find_workflow_stage_builders(find_source_files_under(_SCANNED), _REPO_ROOT)
    assert not offenders, (
        "a `WorkflowStage` pairs a stage with schemas that must be true of it. Only "
        "two places may decide those: app/models/workflow.py resolves them from the "
        "graph, and app/runtime/stage_tests.py takes them from the stage's own "
        "signature so an authored test runs against exactly what the stage declares. "
        "Anywhere else, ask `Workflow` for the stage — `find_workflow_stage` or "
        "`list_workflow_stages` — rather than assembling schemas here:\n  "
        + "\n  ".join(offenders)
    )


def test_both_builders_still_build_one() -> None:
    idle = [
        entry
        for entry in _BUILDERS
        if not find_workflow_stage_constructions(parse_module(_REPO_ROOT / entry))
    ]
    assert not idle, (
        "a _BUILDERS entry constructs no `WorkflowStage`, so it grants a permission "
        "nobody uses and the rule is looser than it reads — drop it:\n  "
        + "\n  ".join(idle)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_find_workflow_stage_constructions_flags_a_call() -> None:
    source = "w = WorkflowStage(stage=s, inputs=[], output_schema=None)\n"
    assert find_workflow_stage_constructions(ast.parse(source)) == [1]


def test_find_workflow_stage_constructions_flags_a_dotted_call() -> None:
    source = "w = workflow_stage.WorkflowStage(stage=s, inputs=[], output_schema=None)\n"
    assert find_workflow_stage_constructions(ast.parse(source)) == [1]


def test_find_workflow_stage_constructions_flags_a_nested_call() -> None:
    source = "ws = [WorkflowStage(stage=s, inputs=[], output_schema=None) for s in stages]\n"
    assert find_workflow_stage_constructions(ast.parse(source)) == [1]


def test_find_workflow_stage_constructions_allows_workflow_stage_input() -> None:
    source = "i = WorkflowStageInput(id='a', table_schema=schema)\n"
    assert find_workflow_stage_constructions(ast.parse(source)) == []


def test_find_workflow_stage_constructions_allows_an_annotation() -> None:
    assert find_workflow_stage_constructions(ast.parse("def f() -> WorkflowStage: ...\n")) == []


def test_find_workflow_stage_constructions_allows_the_import() -> None:
    source = "from app.models.workflow_stage import WorkflowStage\n"
    assert find_workflow_stage_constructions(ast.parse(source)) == []


def test_find_workflow_stage_builders_reports_repo_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "app" / "web" / "panel.py"
    target.parent.mkdir(parents=True)
    target.write_text("w = WorkflowStage(stage=s, inputs=[], output_schema=None)\n", encoding="utf-8")
    assert find_workflow_stage_builders([target], tmp_path) == ["app/web/panel.py:1"]


def test_find_workflow_stage_builders_skips_a_builder(tmp_path: Path) -> None:
    target = tmp_path / "app" / "models" / "workflow.py"
    target.parent.mkdir(parents=True)
    target.write_text("w = WorkflowStage(stage=s, inputs=[], output_schema=None)\n", encoding="utf-8")
    assert find_workflow_stage_builders([target], tmp_path) == []
