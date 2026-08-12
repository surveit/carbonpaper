"""A class an agent is handed a JSON schema of carries NO docstring — not one, ever.
`model_json_schema()` copies a class docstring into the schema's `description`, so prose
written for a maintainer silently becomes prompt. Model-facing wording goes in
`json_schema_extra["description"]`; a note for the reader goes in a comment ABOVE the
class, which no schema can reach. Sibling of test_tool_descriptions_are_explicit.py.
"""
from __future__ import annotations

import ast
import inspect
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from app.models.named_schemas import SchemaLibrary
from app.models.review_guide import ReviewGuideDraft
from app.models.stages.stage_base import StageTest
from app.tools.submitted_stage import SubmittedStage

_REPO_ROOT = Path(__file__).resolve().parents[2]
_APP_ROOT = _REPO_ROOT / "app"

# Every class an agent is handed a JSON schema of: the `add_stage` tools bind their
# argument to SubmittedStage (whose fields are StageDraft's, inherited), and the three
# agents in app/compiler submit through a target_schema. Each root pulls in its whole
# nested model graph. Hand-written, and
# `test_every_schema_root_declared_in_source_is_listed` is what keeps it honest.
_SCHEMA_ROOTS: tuple[type[BaseModel], ...] = (
    SubmittedStage,
    SchemaLibrary,
    ReviewGuideDraft,
    StageTest,
)

# A target_schema the source computes rather than names, each mapped to the listed root
# that stands in for it. AST cannot resolve these, so they are acknowledged here or the
# static check fails: `build_stage_tests_model` returns a suite of StageTest subclasses,
# and app/runtime/llm.py is handed a model built from a stage's own output schema, which
# carries no docstring because `create_model` sets `__doc__` to None.
_DYNAMIC_ROOTS: dict[str, str] = {
    "build_stage_tests_model(...)": "StageTest",
    "target_schema": "a create_model() schema — no class, no docstring",
}

# pydantic's own placeholder for an undocumented Enum; it never reaches a schema.
_ENUM_DEFAULT_DOC = "An enumeration."


def find_classes_reachable_from(root: type[BaseModel]) -> set[type]:
    found: set[type] = set()
    _collect(root, found)
    return found


def find_docstringed_schema_classes() -> list[str]:
    offenders = []
    for root in _SCHEMA_ROOTS:
        for cls in find_classes_reachable_from(root):
            if _ships_a_docstring(cls):
                offenders.append(f"{cls.__module__}::{cls.__qualname__}")
    return sorted(set(offenders))


def find_schema_roots_named_in_source(app_root: Path) -> tuple[set[str], set[str]]:
    """(classes named as a root, unresolvable expressions) — read from source, not imported."""
    named: set[str] = set()
    computed: set[str] = set()
    for path in sorted(app_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                named, computed = _read_target_schema_kwarg(node, named, computed)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                named |= _read_tool_argument_types(node)
    return named, computed


def test_no_class_in_a_tool_schema_describes_itself_in_a_docstring() -> None:
    offenders = find_docstringed_schema_classes()
    assert not offenders, (
        "these classes are handed to a model as JSON schema, and pydantic copies a class "
        "docstring into the schema's `description` — so the docstring IS prompt text, and "
        "editing it edits a prompt silently. This covers Enums too: pydantic reads "
        "`enum_type.__doc__` the same way. A docstring here is refused even when an "
        "explicit description would override it: the rule is that prose a maintainer writes "
        "must not be ABLE to reach a model. Model-facing wording goes in `model_config = "
        "ConfigDict(json_schema_extra={'description': ...})`, with the string in "
        "app/models/tool_schema_prompts.py. A note for the next reader goes in a comment "
        "ABOVE the class, where no schema can reach it:\n  "
        + "\n  ".join(offenders)
    )


def test_every_schema_root_declared_in_source_is_listed() -> None:
    named, computed = find_schema_roots_named_in_source(_APP_ROOT)
    listed = {root.__name__ for root in _SCHEMA_ROOTS}
    assert not named - listed, (
        "app/ names these as a tool argument type or an Agent target_schema, but "
        "_SCHEMA_ROOTS does not list them — so their nested models are handed to an agent "
        "with nothing checking them. This is the hole the reachability walk cannot see for "
        "itself: it only ever walks the roots it is given. Add them to _SCHEMA_ROOTS:\n  "
        + "\n  ".join(sorted(named - listed))
    )
    assert not computed - set(_DYNAMIC_ROOTS), (
        "a target_schema that source cannot resolve to a class. It may be perfectly safe, "
        "but nothing here can tell — say which listed root covers it by adding it to "
        "_DYNAMIC_ROOTS with a reason:\n  " + "\n  ".join(sorted(computed - set(_DYNAMIC_ROOTS)))
    )


def test_the_rule_governs_a_non_empty_set_of_classes() -> None:
    reachable = {c for root in _SCHEMA_ROOTS for c in find_classes_reachable_from(root)}
    assert len(reachable) > 20 and any(issubclass(c, Enum) for c in reachable), (
        "the reachability walk found almost nothing, or no Enum at all, so this rule would "
        f"pass vacuously — it reached only {sorted(c.__name__ for c in reachable)}"
    )


def test_the_static_root_scan_finds_the_roots_it_should() -> None:
    named, computed = find_schema_roots_named_in_source(_APP_ROOT)
    assert {"SubmittedStage", "SchemaLibrary", "ReviewGuideDraft"} <= named, (
        f"the source scan stopped seeing declared roots — it found {sorted(named)}"
    )
    assert computed, "the scan should still be flagging the two computed target_schemas"


def test_a_class_carrying_only_an_explicit_description_is_accepted() -> None:
    from app.models.stages.union import UnionConfig

    assert UnionConfig.__doc__ is None
    assert "app.models.stages.union::UnionConfig" not in find_docstringed_schema_classes()


def test_an_explicit_description_does_not_buy_back_a_docstring() -> None:
    class Both(BaseModel):
        """Prose a maintainer wrote, which pydantic would ship."""

        model_config = ConfigDict(json_schema_extra={"description": "what the model reads"})

    extra = Both.model_config.get("json_schema_extra")
    assert isinstance(extra, dict) and "description" in extra
    assert _ships_a_docstring(Both)


def test_an_undocumented_enum_is_not_reported() -> None:
    class Verdict(str, Enum):
        approve = "approve"

    assert not _ships_a_docstring(Verdict)


def test_a_documented_enum_is_reported() -> None:
    class Verdict(str, Enum):
        """Prose that pydantic copies into the enum's schema description."""

        approve = "approve"

    assert _ships_a_docstring(Verdict)


def _ships_a_docstring(cls: type) -> bool:
    doc = cls.__doc__
    if not doc:
        return False
    return not (issubclass(cls, Enum) and inspect.cleandoc(doc) == _ENUM_DEFAULT_DOC)


def _collect(model: type[BaseModel], found: set[type]) -> None:
    if model in found:
        return
    found.add(model)
    for field in model.model_fields.values():
        stack = [field.annotation]
        while stack:
            annotation = stack.pop()
            stack.extend(getattr(annotation, "__args__", ()) or ())
            if not isinstance(annotation, type):
                continue
            if issubclass(annotation, BaseModel):
                _collect(annotation, found)
            elif issubclass(annotation, Enum):
                found.add(annotation)
    for subclass in model.__subclasses__():
        _collect(subclass, found)


def _read_target_schema_kwarg(
    call: ast.Call, named: set[str], computed: set[str]
) -> tuple[set[str], set[str]]:
    for keyword in call.keywords:
        if keyword.arg != "target_schema":
            continue
        name = _class_name_of(keyword.value)
        if name is not None and name[:1].isupper():
            named = named | {name}
        else:
            computed = computed | {_describe_expression(keyword.value)}
    return named, computed


def _read_tool_argument_types(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    if function.name != "add_stage":
        return set()
    names = {_class_name_of(arg.annotation) for arg in function.args.args if arg.annotation}
    return {name for name in names if name is not None and name[:1].isupper()}


def _class_name_of(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _class_name_of(node.slice)
    return None


def _describe_expression(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return f"{_class_name_of(node.func) or ast.unparse(node.func)}(...)"
    return ast.unparse(node)
