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

from app.models.stage import SERVER_OWNED_STAGE_FIELDS, StageDraft
from app.services import drafts
from app.services import project as project_service
from app.services.drafts import DraftEdit


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


def edit_stage_reporting_drops(
    project_id: str, stage_id: str, changes_json: str
) -> dict[str, Any]:
    trimmed, dropped = _drop_server_owned_from_json(changes_json)
    result = project_service.edit_stage(project_id, stage_id, trimmed)
    reported: dict[str, Any] = {"ok": result.ok, "issues": result.issues}
    warnings = _describe_dropped_fields(stage_id, dropped)
    if warnings:
        reported["warnings"] = warnings
    return reported


def set_draft_stage_reporting_drops(
    project_id: str, draft_id: str, stage_json: str
) -> DraftEdit:
    """The draft's reply carries no warnings channel, so a drop is reported as an issue."""
    trimmed, dropped = _drop_server_owned_from_json(stage_json)
    edit = drafts.set_draft_stage(project_id, draft_id, trimmed)
    edit.issues = edit.issues + _describe_dropped_fields(_read_id(trimmed), dropped)
    return edit


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


def _read_id(stage_json: str) -> str:
    submitted = _parse_object(stage_json) or {}
    stage_id = submitted.get("id")
    return stage_id if isinstance(stage_id, str) else "?"


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
