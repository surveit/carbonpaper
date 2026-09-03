"""What the stage-writing tools receive, and the trimming that keeps a copied stage usable.

A client that pastes back a stage it read carries fields only the server writes.
Dropping them beats refusing the whole stage, but it accommodates the sender rather
than describing a stage, so it stays here and out of `StageDraft`.
"""
from __future__ import annotations

import json
from typing import Any, Sequence

from pydantic import Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from app.models.stage import SERVER_OWNED_STAGE_FIELDS, StageDraft, StageEdit
from app.tools.shared import EditedStages
from app.services import project as project_service, stage_edit


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
    store: stage_edit.StageSpecStore, stages: Sequence[SubmittedStage]
) -> dict[str, Any]:
    result = project_service.add_stages_reporting_outcome(store, stages)
    warnings = _find_dropped_field_warnings(stages, result["added"])
    if warnings:
        result["warnings"] = warnings
    return result


def edit_stages_reporting_drops(
    store: stage_edit.StageSpecStore, edits: Sequence[StageEdit]
) -> EditedStages:
    trimmed: list[StageEdit] = []
    warnings: list[str] = []
    for edit in edits:
        changes, dropped = _drop_server_owned_from_json(edit.changes_json)
        trimmed.append(StageEdit(stage_id=edit.stage_id, changes_json=changes))
        warnings += _describe_dropped_fields(edit.stage_id, dropped)
    result = project_service.edit_stages(store, trimmed)
    return EditedStages(
        ok=result.ok,
        edited=[edit.stage_id for edit in trimmed] if result.ok else [],
        issues=result.issues,
        warnings=warnings,
    )


def _drop_server_owned_from_json(stage_json: str) -> tuple[str, list[str]]:
    """Text that is not a JSON object passes through untouched, so the service states why."""
    submitted = _parse_object(stage_json)
    if submitted is None:
        return stage_json, []
    dropped = [name for name in SERVER_OWNED_STAGE_FIELDS if name in submitted]
    if not dropped:
        return stage_json, []
    kept = {k: v for k, v in submitted.items() if k not in SERVER_OWNED_STAGE_FIELDS}
    return json.dumps(kept), dropped


def _parse_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _describe_dropped_fields(stage_id: str, dropped: Sequence[str]) -> list[str]:
    if not dropped:
        return []
    return [
        f"`{stage_id}`: ignored server-owned fields: {', '.join(dropped)}",
        _WHO_WRITES_THEM,
    ]


_WHO_WRITES_THEM = (
    "only the server writes these: tests come from generate_stage_tests, "
    "review is human-only."
)


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
    return named + [_WHO_WRITES_THEM]
