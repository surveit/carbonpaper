"""Architecture: ``AuthoredStageFields`` is a bundle of field declarations that
``StageDraft`` and ``AbstractStage`` each inherit for reuse — nothing is a *kind of* it,
so it must never be annotated. Subclassing it is the one sanctioned use; a parameter,
return, variable or ``TypeVar`` bound takes the ``StageInGraph`` protocol
(app/models/workflow.py) or the concrete ``Stage``/``StageDraft``.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import find_annotation_name_linenos, parse_module
from arch.scope import find_source_files_under

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = (_REPO_ROOT / "app", _REPO_ROOT / "tests")
_NAME = "AuthoredStageFields"


def find_annotated_uses(tree: ast.Module, name: str) -> list[int]:
    linenos: set[int] = set()
    for node in ast.walk(tree):
        for expression in _annotation_expressions(node):
            linenos.update(find_annotation_name_linenos(expression, frozenset({name})))
    return sorted(linenos)


def find_annotated_use_offenders(paths: list[Path], repo_root: Path, name: str) -> list[str]:
    return [
        f"{path.relative_to(repo_root).as_posix()}:{lineno}"
        for path in paths
        for lineno in find_annotated_uses(parse_module(path), name)
    ]


def _annotation_expressions(node: ast.AST) -> list[ast.expr]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [node.returns] if node.returns is not None else []
    if isinstance(node, ast.arg) and node.annotation is not None:
        return [node.annotation]
    if isinstance(node, ast.AnnAssign):
        return [node.annotation]
    if isinstance(node, ast.Call) and _is_typevar_call(node):
        return [kw.value for kw in node.keywords if kw.arg == "bound"]
    return []


def _is_typevar_call(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "TypeVar"
    return isinstance(func, ast.Attribute) and func.attr == "TypeVar"


def test_authored_stage_fields_is_never_annotated() -> None:
    paths = [path for target in _SCANNED for path in find_source_files_under(target)]
    offenders = find_annotated_use_offenders(paths, _REPO_ROOT, _NAME)
    assert not offenders, (
        f"`{_NAME}` is a bundle of field declarations, not a type — it is `AbstractStage` "
        "minus SERVER_OWNED_STAGE_FIELDS, and nothing is a *kind of* it. Subclassing it is "
        "the only sanctioned use. Annotate the `StageInGraph` protocol in "
        "app/models/workflow.py when the code reads only a stage's id and inputs, or the "
        "concrete `Stage`/`StageDraft` otherwise:\n  " + "\n  ".join(offenders)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_find_annotated_uses_flags_a_parameter_annotation() -> None:
    assert find_annotated_uses(ast.parse("def f(s: AuthoredStageFields) -> None: ...\n"), _NAME) == [1]


def test_find_annotated_uses_flags_a_return_annotation() -> None:
    assert find_annotated_uses(ast.parse("def f() -> AuthoredStageFields: ...\n"), _NAME) == [1]


def test_find_annotated_uses_flags_a_variable_annotation() -> None:
    assert find_annotated_uses(ast.parse("s: AuthoredStageFields\n"), _NAME) == [1]


def test_find_annotated_uses_flags_a_nested_subscript() -> None:
    tree = ast.parse("def f(s: Optional[list[AuthoredStageFields]]) -> None: ...\n")
    assert find_annotated_uses(tree, _NAME) == [1]


def test_find_annotated_uses_flags_a_typevar_bound() -> None:
    tree = ast.parse('T = TypeVar("T", bound=AuthoredStageFields)\n')
    assert find_annotated_uses(tree, _NAME) == [1]


def test_find_annotated_uses_flags_a_string_annotation() -> None:
    tree = ast.parse('def f(s: "AuthoredStageFields") -> None: ...\n')
    assert find_annotated_uses(tree, _NAME) == [1]


def test_find_annotated_uses_flags_a_dotted_reference() -> None:
    tree = ast.parse("def f(s: stage_base.AuthoredStageFields) -> None: ...\n")
    assert find_annotated_uses(tree, _NAME) == [1]


def test_find_annotated_uses_allows_a_class_base() -> None:
    tree = ast.parse("class StageDraft(AuthoredStageFields):\n    id: str\n")
    assert find_annotated_uses(tree, _NAME) == []


def test_find_annotated_uses_allows_issubclass() -> None:
    tree = ast.parse("assert issubclass(StageDraft, AuthoredStageFields)\n")
    assert find_annotated_uses(tree, _NAME) == []


def test_find_annotated_uses_allows_the_import() -> None:
    tree = ast.parse("from app.models.stage import AuthoredStageFields\n")
    assert find_annotated_uses(tree, _NAME) == []


def test_find_annotated_use_offenders_reports_repo_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "graph.py"
    target.write_text("def f(s: AuthoredStageFields) -> None: ...\n", encoding="utf-8")
    assert find_annotated_use_offenders([target], tmp_path, _NAME) == ["graph.py:1"]
