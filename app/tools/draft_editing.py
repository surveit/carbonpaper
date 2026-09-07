"""The tools that edit a project through a DRAFT, which every agent surface holds its own of."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.services import drafts, project as project_service, versioning
from app.tools.shared import STAGE_TOOL_ERRORS


class DraftHandle(BaseModel):
    draft_id: str
    parent_version: str | None
    stages: list[str]


def start_editing(project_id: str) -> DraftHandle:
    view = drafts.create_draft(
        project_id, from_version=versioning.find_latest_version_id(project_id)
    )
    return DraftHandle(
        draft_id=view.id,
        parent_version=view.parent_version,
        stages=[stage.id for stage in view.stages],
    )


def read_draft_stage(project_id: str, draft_id: str, stage_id: str) -> str:
    specs = drafts.read_draft_specs(project_id, draft_id)
    if stage_id not in specs:
        raise ValueError(f"no stage '{stage_id}' in draft '{draft_id}'")
    return json.dumps(specs[stage_id], indent=2)


def delete_stage(project_id: str, draft_id: str, stage_id: str) -> dict[str, Any]:
    try:
        result = project_service.delete_stage(project_id, draft_id, stage_id)
    except STAGE_TOOL_ERRORS as exc:
        return {"ok": False, "issues": [str(exc)]}
    return {"ok": result.ok, "issues": result.issues}


def save_version(
    project_id: str, draft_id: str, message: str, override_conflict: bool = False
) -> dict[str, Any]:
    result = drafts.save_version(
        project_id, draft_id, message=message, override_conflict=override_conflict
    )
    return result.model_dump(mode="json")
