"""Architecture: ``Stage`` is what an author writes and what we read and write to
storage. Everywhere else holds ``WorkflowStage`` (app/models/workflow_stage.py), the
stage together with the input and output schemas resolved from the whole graph —
naming ``Stage`` in an annotation outside the seam below claims a stage can be
understood on its own. Reaching ``.stage`` off a ``WorkflowStage`` is not a use here.
"""
from __future__ import annotations

import ast
from pathlib import Path

from arch._helpers import parse_module
from arch.scope import find_source_files_under
from arch.test_authored_stage_fields_is_not_a_type import _annotation_expressions

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCANNED = _REPO_ROOT / "app"
_NAME = "Stage"

# The read/write seam: every entry parses an authored stage spec, or serialises one
# back out, so it must name the authored type. Adding an entry is a human decision.
_SEAM = (
    "app/models",                  # defines both Stage and WorkflowStage
    "app/services/loader.py",      # reads the compiled stage JSON off disk
    "app/services/stage_edit.py",  # writes an author's edit of one stage back
    "app/services/drafts.py",
    "app/services/versioning.py",  # stores and loads a version's stage list
    "app/services/project.py",     # writes the stages a new project starts with
    "app/seeds",                   # ships stage specs as authored JSON
    "app/tools",                   # the stage-authoring tools an LLM calls
    "app/mcp",                     # the same authoring tools over MCP
    "app/agents",                  # the agent turns that write stages
    "app/compiler",                # turns an author's prose into stage specs
    # The one place that runs a stage OUTSIDE any workflow: it executes the stage
    # against its own signature, and builds its `WorkflowStage` from that signature
    # rather than from the graph — which is why it is one of the two sanctioned
    # builders (tests/arch/test_workflow_stage_is_built_in_two_places.py).
    "app/runtime/stage_tests.py",
)


def find_stage_annotations(tree: ast.Module) -> list[int]:
    linenos: set[int] = set()
    for node in ast.walk(tree):
        for expression in _annotation_expressions(node):
            linenos.update(_stage_linenos(expression))
    return sorted(linenos)


def find_stage_annotations_outside_the_seam(paths: list[Path], repo_root: Path) -> list[str]:
    offenders: list[str] = []
    for path in paths:
        module_path = path.relative_to(repo_root).as_posix()
        if sits_in_the_seam(module_path):
            continue
        offenders += [f"{module_path}:{lineno}" for lineno in find_stage_annotations(parse_module(path))]
    return offenders


def sits_in_the_seam(module_path: str) -> bool:
    return any(module_path == entry or module_path.startswith(f"{entry}/") for entry in _SEAM)


def _stage_linenos(expression: ast.expr) -> set[int]:
    """Exact identifier: WorkflowStage, StageDraft, StageId, StageTest are other types."""
    linenos: set[int] = set()
    for node in ast.walk(expression):
        if isinstance(node, (ast.Name, ast.Attribute)) and _names_stage(node):
            linenos.add(node.lineno)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _string_annotation_names_stage(node.value):
                linenos.add(node.lineno)
    return linenos


def _names_stage(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id == _NAME
    return isinstance(node, ast.Attribute) and node.attr == _NAME


def _string_annotation_names_stage(text: str) -> bool:
    try:
        inner = ast.parse(text, mode="eval").body
    except SyntaxError:
        return False
    return any(_names_stage(node) for node in ast.walk(inner))


def test_stage_is_annotated_only_at_the_read_write_seam() -> None:
    paths = find_source_files_under(_SCANNED)
    offenders = find_stage_annotations_outside_the_seam(paths, _REPO_ROOT)
    assert not offenders, (
        "`Stage` is what an author writes and what we read and write to storage. A "
        "stage's input and output schemas are a function of the whole workflow, so "
        "code that runs, renders or reasons about a stage takes `WorkflowStage` "
        "(app/models/workflow_stage.py) and reaches `.stage` for the authored fields. "
        "Take the workflow stage from `Workflow.find_workflow_stage` / "
        "`list_workflow_stages` instead of the bare `Stage` at:\n  "
        + "\n  ".join(offenders)
    )


def test_every_seam_entry_exists() -> None:
    missing = [entry for entry in _SEAM if not (_REPO_ROOT / entry).exists()]
    assert not missing, (
        "a _SEAM entry names no file or package, so it exempts nothing and hides "
        "whatever moved into its place — delete it or point it at the new path:\n  "
        + "\n  ".join(missing)
    )


# --- unit tests for the finder, on inline snippets (red + green) ---------


def test_find_stage_annotations_flags_a_parameter_annotation() -> None:
    assert find_stage_annotations(ast.parse("def f(s: Stage) -> None: ...\n")) == [1]


def test_find_stage_annotations_flags_a_return_annotation() -> None:
    assert find_stage_annotations(ast.parse("def f() -> Stage: ...\n")) == [1]


def test_find_stage_annotations_flags_a_variable_annotation() -> None:
    assert find_stage_annotations(ast.parse("s: Stage\n")) == [1]


def test_find_stage_annotations_flags_a_nested_subscript() -> None:
    assert find_stage_annotations(ast.parse("def f(s: Optional[list[Stage]]) -> None: ...\n")) == [1]


def test_find_stage_annotations_flags_a_dict_value() -> None:
    assert find_stage_annotations(ast.parse("s: dict[str, Stage]\n")) == [1]


def test_find_stage_annotations_flags_a_typevar_bound() -> None:
    assert find_stage_annotations(ast.parse('T = TypeVar("T", bound=Stage)\n')) == [1]


def test_find_stage_annotations_flags_a_string_annotation() -> None:
    assert find_stage_annotations(ast.parse('def f(s: "Stage") -> None: ...\n')) == [1]


def test_find_stage_annotations_flags_a_dotted_reference() -> None:
    assert find_stage_annotations(ast.parse("def f(s: stage.Stage) -> None: ...\n")) == [1]


def test_find_stage_annotations_allows_workflow_stage() -> None:
    assert find_stage_annotations(ast.parse("def f(s: WorkflowStage) -> None: ...\n")) == []


def test_find_stage_annotations_allows_a_string_annotation_naming_workflow_stage() -> None:
    assert find_stage_annotations(ast.parse('def f(s: "list[WorkflowStage]") -> None: ...\n')) == []


def test_find_stage_annotations_allows_the_other_stage_prefixed_types() -> None:
    source = "def f(a: StageDraft, b: StageId, c: StageTest, d: StageType) -> AbstractStage: ...\n"
    assert find_stage_annotations(ast.parse(source)) == []


def test_find_stage_annotations_allows_reading_dot_stage() -> None:
    assert find_stage_annotations(ast.parse("def f(w: WorkflowStage) -> None:\n    return w.stage\n")) == []


def test_find_stage_annotations_allows_the_import() -> None:
    assert find_stage_annotations(ast.parse("from app.models.stage import Stage\n")) == []


def test_sits_in_the_seam_matches_a_package_prefix() -> None:
    assert sits_in_the_seam("app/models/stages/join.py") is True


def test_sits_in_the_seam_matches_a_named_file() -> None:
    assert sits_in_the_seam("app/services/loader.py") is True


def test_sits_in_the_seam_rejects_a_sibling_of_a_named_file() -> None:
    assert sits_in_the_seam("app/services/run.py") is False


def test_sits_in_the_seam_rejects_a_package_sharing_a_name_prefix() -> None:
    assert sits_in_the_seam("app/models_extra/thing.py") is False


def test_find_stage_annotations_outside_the_seam_reports_repo_relative_path(tmp_path: Path) -> None:
    target = tmp_path / "app" / "web" / "panel.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def f(s: Stage) -> None: ...\n", encoding="utf-8")
    assert find_stage_annotations_outside_the_seam([target], tmp_path) == ["app/web/panel.py:1"]


def test_find_stage_annotations_outside_the_seam_skips_a_seam_module(tmp_path: Path) -> None:
    target = tmp_path / "app" / "services" / "loader.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("def f(s: Stage) -> None: ...\n", encoding="utf-8")
    assert find_stage_annotations_outside_the_seam([target], tmp_path) == []
