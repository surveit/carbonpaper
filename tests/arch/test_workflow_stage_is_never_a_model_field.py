"""Architecture: a ``WorkflowStage`` is in-memory only, and a pydantic field is the only
route to storage. The class is a frozen stdlib dataclass, which pydantic v2 accepts as a
field type — so a model declaring one really would dump it to JSON, freezing schemas the
``Workflow`` recomputes from the whole graph. Detection is AST-only, importing nothing.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import find_annotation_name_linenos, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = _REPO_ROOT / "app"
_NAMES = frozenset({"WorkflowStage", "WorkflowStageInput"})


def find_declared_fields(tree: ast.Module) -> list[tuple[int, str]]:
    fields: list[tuple[int, str]] = []
    for class_node in _find_undecorated_classes(tree):
        for stmt in class_node.body:
            if isinstance(stmt, ast.AnnAssign) and find_annotation_name_linenos(
                stmt.annotation, _NAMES
            ):
                fields.append((stmt.lineno, f"class {class_node.name}.{_target_name(stmt)}"))
    return sorted(fields)


def find_declared_field_offenders(paths: list[Path], repo_root: Path) -> list[str]:
    return [
        f"{path.relative_to(repo_root).as_posix()}:{lineno}  {label}"
        for path in paths
        for lineno, label in find_declared_fields(parse_module(path))
    ]


def _find_undecorated_classes(tree: ast.Module) -> list[ast.ClassDef]:
    # A @dataclass declaring these is the sanctioned shape: WorkflowStage holds inputs.
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and not _is_dataclass(node)
    ]


def _is_dataclass(node: ast.ClassDef) -> bool:
    return any(_decorator_name(item) == "dataclass" for item in node.decorator_list)


def _decorator_name(decorator: ast.expr) -> str | None:
    inner = decorator.func if isinstance(decorator, ast.Call) else decorator
    if isinstance(inner, ast.Name):
        return inner.id
    return inner.attr if isinstance(inner, ast.Attribute) else None


def _target_name(stmt: ast.AnnAssign) -> str:
    return stmt.target.id if isinstance(stmt.target, ast.Name) else ast.unparse(stmt.target)


def test_workflow_stage_is_never_a_model_field() -> None:
    offenders = find_declared_field_offenders(find_source_files_under(_SCANNED), _REPO_ROOT)
    assert not offenders, (
        "a `WorkflowStage` is a `Stage` plus the schemas its `Workflow` works out from "
        "the whole graph, so it is built at the point of use and never stored. Pydantic "
        "v2 accepts a frozen stdlib dataclass as a field type, so this field really does "
        "round-trip through the store, freezing schemas that must be recomputed whenever "
        "an upstream stage changes. Persist the `Stage` and ask the `Workflow` again — "
        "`find_workflow_stage` / `list_workflow_stages` (app/models/workflow.py). Every "
        "class body counts, not only pydantic subclasses: a base class is not decidable "
        "from AST alone, and `@dataclass` is the one shape excluded:\n  "
        + "\n  ".join(offenders)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_find_declared_fields_flags_a_bare_field() -> None:
    source = "class RunView(BaseModel):\n    stage: WorkflowStage\n"
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.stage")]


def test_find_declared_fields_flags_a_field_with_a_default() -> None:
    source = "class RunView(BaseModel):\n    stage: WorkflowStage = None\n"
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.stage")]


def test_find_declared_fields_flags_a_list_of_inputs() -> None:
    source = "class RunView(PersistedModel):\n    items: list[WorkflowStageInput]\n"
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.items")]


def test_find_declared_fields_flags_an_optional() -> None:
    source = "class RunView(_Base):\n    stage: Optional[WorkflowStage] = None\n"
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.stage")]


def test_find_declared_fields_flags_a_string_annotation() -> None:
    source = 'class RunView(BaseModel):\n    stage: "WorkflowStage"\n'
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.stage")]


def test_find_declared_fields_flags_a_dotted_reference() -> None:
    source = "class RunView(BaseModel):\n    stage: workflow_stage.WorkflowStage\n"
    assert find_declared_fields(ast.parse(source)) == [(2, "class RunView.stage")]


def test_find_declared_fields_flags_a_class_nested_in_a_dataclass() -> None:
    source = (
        "@dataclass(frozen=True)\n"
        "class Outer:\n"
        "    class Inner(BaseModel):\n"
        "        stage: WorkflowStage\n"
    )
    assert find_declared_fields(ast.parse(source)) == [(4, "class Inner.stage")]


def test_find_declared_fields_allows_the_dataclass_that_declares_them() -> None:
    source = (
        "@dataclass(frozen=True)\n"
        "class WorkflowStage:\n"
        '    stage: "Stage"\n'
        "    inputs: list[WorkflowStageInput]\n"
    )
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_fields_allows_a_function_parameter() -> None:
    source = "class Panel:\n    def render(self, stage: WorkflowStage) -> None: ...\n"
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_fields_allows_a_function_return() -> None:
    source = "class Panel:\n    def resolve(self) -> WorkflowStage: ...\n"
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_fields_allows_a_local_variable_annotation() -> None:
    source = (
        "class Panel:\n"
        "    def render(self) -> None:\n"
        "        stage: WorkflowStage = self.resolve()\n"
    )
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_fields_allows_a_plain_class_body_assignment() -> None:
    source = "class Panel:\n    stage = WorkflowStage\n"
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_fields_allows_the_import() -> None:
    source = "from app.models.workflow_stage import WorkflowStage\n"
    assert find_declared_fields(ast.parse(source)) == []


def test_find_declared_field_offenders_reports_repo_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "app" / "web" / "panel.py"
    target.parent.mkdir(parents=True)
    target.write_text("class Panel(BaseModel):\n    stage: WorkflowStage\n", encoding="utf-8")
    assert find_declared_field_offenders([target], tmp_path) == [
        "app/web/panel.py:2  class Panel.stage"
    ]
