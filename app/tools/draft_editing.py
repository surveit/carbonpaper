"""The tools that edit a project through a DRAFT, which every agent surface holds its own of."""

from __future__ import annotations

import json

from pydantic import BaseModel

from app.services import drafts, project as project_service, versioning
from app.services.drafts import SaveResult as SaveResult
from app.services.stage_edit import EditStageResult as EditStageResult
from app.services.workspace import StageSummary
from app.tools.shared import STAGE_TOOL_ERRORS, validate_project_exists


class WorkflowDraft(BaseModel):
    draft_id: str
    stages: list[StageSummary]
    # What save_version would refuse this draft for, empty while it would be accepted.
    issues: list[str]


def start_editing(project_id: str) -> str:
    validate_project_exists(project_id)
    newest = versioning.find_latest_version_id(project_id)
    return drafts.create_draft(project_id, from_version_id=newest).id


def read_workflow_draft(project_id: str, draft_id: str) -> WorkflowDraft:
    validate_project_exists(project_id)
    detail = drafts.read_draft(project_id, draft_id)
    return WorkflowDraft(
        draft_id=detail.id,
        stages=[
            StageSummary(
                id=stage.id,
                type=stage.type,
                description=stage.description,
                inputs=[ref.id for ref in stage.inputs],
            )
            for stage in detail.stages
        ],
        issues=detail.issues,
    )


def read_draft_stage(project_id: str, draft_id: str, stage_id: str) -> str:
    validate_project_exists(project_id)
    stages = drafts.read_draft_stages(project_id, draft_id)
    if stage_id not in stages:
        raise ValueError(f"no stage '{stage_id}' in draft '{draft_id}'")
    return json.dumps(stages[stage_id], indent=2)


def delete_stage(project_id: str, draft_id: str, stage_id: str) -> EditStageResult:
    validate_project_exists(project_id)
    try:
        return project_service.delete_stage(project_id, draft_id, stage_id)
    except STAGE_TOOL_ERRORS as exc:
        return EditStageResult(ok=False, issues=[str(exc)])


def save_version(
    project_id: str, draft_id: str, message: str, override_conflict: bool = False
) -> SaveResult:
    validate_project_exists(project_id)
    return drafts.save_version(
        project_id, draft_id, message=message, override_conflict=override_conflict
    )
