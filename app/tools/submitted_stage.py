"""What add_stage actually receives, and the trimming that keeps a copied stage usable.

A client that pastes back a stage it read carries fields only the server writes.
Dropping them beats refusing the whole stage, but it accommodates the sender rather
than describing a stage, so it stays here and out of `StageDraft`.
"""
from __future__ import annotations

from typing import Any, Sequence

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from app.models.stage import SERVER_OWNED_STAGE_FIELDS, StageDraft
from app.services import project as project_service


# The strip runs BEFORE validation because both tool surfaces bind this model as an
# argument type: by the time a handler body runs, an undeclared field has already failed
# the call. Add no cross-field rule here for the same reason — one belongs on `Stage`,
# where it comes back as an issue instead of a bad argument.
# `dropped_server_owned_fields` records what one submission carried, for the caller to
# warn about. Bookkeeping about a submission, not part of a stage: kept out of the JSON
# schema a client is handed and out of every dump.
class SubmittedStage(StageDraft):
    dropped_server_owned_fields: SkipJsonSchema[list[str]] = Field(
        default_factory=list, exclude=True
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_server_owned_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        present = [name for name in SERVER_OWNED_STAGE_FIELDS if name in data]
        remaining = {k: v for k, v in data.items() if k not in SERVER_OWNED_STAGE_FIELDS}
        remaining["dropped_server_owned_fields"] = present
        return remaining


def add_stages_reporting_drops(
    project_id: str, stages: Sequence[SubmittedStage]
) -> dict[str, Any]:
    result = project_service.add_stages_reporting_outcome(project_id, stages)
    warnings = _find_dropped_field_warnings(stages, result["added"])
    if warnings:
        result["warnings"] = warnings
    return result


def _find_dropped_field_warnings(
    stages: Sequence[SubmittedStage], added: list[str]
) -> list[str]:
    stored = set(added)
    named = [
        f"`{s.id}`: ignored server-owned fields: {', '.join(s.dropped_server_owned_fields)}"
        for s in stages
        if s.id in stored and s.dropped_server_owned_fields
    ]
    if not named:
        return []
    return named + [
        "only the server writes these: tests come from generate_stage_tests, "
        "review is human-only."
    ]
