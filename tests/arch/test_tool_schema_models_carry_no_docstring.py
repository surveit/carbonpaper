"""A model an agent is handed a JSON schema of carries NO docstring — not one, ever.
`model_json_schema()` copies a class docstring into the schema's `description`, so prose
written for a maintainer silently becomes prompt. Model-facing wording goes in
`json_schema_extra["description"]`; a note for the reader goes in a comment ABOVE the
class, which no schema can reach. Sibling of test_tool_descriptions_are_explicit.py.
"""
from __future__ import annotations


from pydantic import BaseModel, ConfigDict

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
            if model.__doc__:
                offenders.append(f"{model.__module__}::{model.__qualname__}")
    return sorted(set(offenders))


def test_no_model_in_a_tool_schema_describes_itself_in_a_docstring() -> None:
    offenders = find_docstringed_schema_models()
    assert not offenders, (
        "these classes are handed to a model as JSON schema, and pydantic copies a class "
        "docstring into the schema's `description` — so the docstring IS prompt text, and "
        "editing it edits a prompt silently. A docstring here is refused even when an "
        "explicit description would override it: the rule is that prose a maintainer writes "
        "must not be ABLE to reach a model. Model-facing wording goes in `model_config = "
        "ConfigDict(json_schema_extra={'description': ...})`, with the string in "
        "app/models/tool_schema_prompts.py. A note for the next reader goes in a comment "
        "ABOVE the class, where no schema can reach it:\n  "
        + "\n  ".join(offenders)
    )


def test_the_rule_governs_a_non_empty_set_of_models() -> None:
    reachable = {m for root in _SCHEMA_ROOTS for m in find_models_reachable_from(root)}
    assert len(reachable) > 20, (
        "the reachability walk found almost nothing, so this rule would pass vacuously — "
        f"it reached only {sorted(m.__name__ for m in reachable)}"
    )


def test_a_model_carrying_only_an_explicit_description_is_accepted() -> None:
    from app.models.stages.union import UnionConfig

    assert UnionConfig.__doc__ is None
    assert "app.models.stages.union::UnionConfig" not in find_docstringed_schema_models()


def test_an_explicit_description_does_not_buy_back_a_docstring() -> None:
    class Both(BaseModel):
        """Prose a maintainer wrote, which pydantic would ship."""

        model_config = ConfigDict(json_schema_extra={"description": "what the model reads"})

    extra = Both.model_config.get("json_schema_extra")
    assert isinstance(extra, dict) and "description" in extra
    assert Both.__doc__ is not None


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

