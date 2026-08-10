"""A model an agent is handed a JSON schema of must not describe itself in a docstring.
`model_json_schema()` copies a class docstring into the schema's `description`, so a
docstring edit is a prompt edit no reviewer reads as one — the reason
tests/arch/test_tool_descriptions_are_explicit.py already gives for tool descriptions.
The carrier here is `model_config`'s `json_schema_extra["description"]`.
"""
from __future__ import annotations


from pydantic import BaseModel

from app.models import StageDraft
from app.models.named_schemas import SchemaLibrary
from app.models.review_guide import ReviewGuideDraft
from app.models.stages.stage_base import StageTest

# Every model an agent is handed a JSON schema of: the `add_stage` tools bind their
# argument to StageDraft, and the three agents in app/compiler submit through a
# target_schema. Each root pulls in its whole nested model graph.
_SCHEMA_ROOTS: tuple[type[BaseModel], ...] = (
    StageDraft,
    SchemaLibrary,
    ReviewGuideDraft,
    StageTest,
)


def find_models_reachable_from(root: type[BaseModel]) -> set[type[BaseModel]]:
    found: set[type[BaseModel]] = set()
    _collect(root, found)
    return found


def find_docstringed_schema_models() -> list[str]:
    offenders = []
    for root in _SCHEMA_ROOTS:
        for model in find_models_reachable_from(root):
            if model.__doc__ and not _declares_its_description(model):
                offenders.append(f"{model.__module__}::{model.__qualname__}")
    return sorted(set(offenders))


def test_no_model_in_a_tool_schema_describes_itself_in_a_docstring() -> None:
    offenders = find_docstringed_schema_models()
    assert not offenders, (
        "these classes are handed to a model as JSON schema, and pydantic copies a class "
        "docstring into the schema's `description` — so the docstring IS prompt text, and "
        "editing it edits a prompt silently. Move the model-facing wording to an explicit "
        "`model_config = ConfigDict(json_schema_extra={'description': ...})` (the strings "
        "live in app/models/tool_schema_prompts.py), and delete the docstring — or keep a "
        "reader-facing docstring only alongside that explicit description, which wins:\n  "
        + "\n  ".join(offenders)
    )


def test_the_rule_governs_a_non_empty_set_of_models() -> None:
    reachable = {m for root in _SCHEMA_ROOTS for m in find_models_reachable_from(root)}
    assert len(reachable) > 20, (
        "the reachability walk found almost nothing, so this rule would pass vacuously — "
        f"it reached only {sorted(m.__name__ for m in reachable)}"
    )


def test_a_model_declaring_its_description_explicitly_is_accepted() -> None:
    from app.models.stages.union import UnionConfig

    assert _declares_its_description(UnionConfig)
    assert "app.models.stages.union::UnionConfig" not in find_docstringed_schema_models()


def test_a_bare_docstringed_model_is_reported() -> None:
    class Described(BaseModel):
        """Prose that would reach the model."""

    assert not _declares_its_description(Described)


def _collect(model: type[BaseModel], found: set[type[BaseModel]]) -> None:
    if model in found:
        return
    found.add(model)
    for field in model.model_fields.values():
        stack = [field.annotation]
        while stack:
            annotation = stack.pop()
            stack.extend(getattr(annotation, "__args__", ()) or ())
            if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                _collect(annotation, found)
    for subclass in model.__subclasses__():
        _collect(subclass, found)


def _declares_its_description(model: type[BaseModel]) -> bool:
    extra = model.model_config.get("json_schema_extra")
    return isinstance(extra, dict) and "description" in extra
