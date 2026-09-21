"""One project's stages, flattened for a reader that wants names rather than specs."""
from __future__ import annotations

from pydantic import BaseModel

from app.models import StageType
from app.services.loader import load_stage_entries
from app.services.workspace import validate_project_id


class StageSummary(BaseModel):
    id: str
    type: StageType
    description: str
    inputs: list[str]


class WorkflowSummary(BaseModel):
    name: str
    stages: list[StageSummary]
    issues: list[str]  # one per stage that would not parse; it is absent from `stages`


def project_workflow_summary(project_id: str) -> WorkflowSummary:
    stages: list[StageSummary] = []
    issues: list[str] = []
    for entry in load_stage_entries(project_id):
        if entry.stage is None:
            issues.append(f"{entry.label}: {'; '.join(entry.issues)}")
            continue
        stage = entry.stage
        stages.append(StageSummary(
            id=stage.id,
            type=stage.type,
            description=stage.description,
            inputs=[ref.id for ref in stage.inputs],
        ))
    return WorkflowSummary(name=project_id, stages=stages, issues=issues)


def read_workflow_summary(name: str) -> WorkflowSummary:
    return project_workflow_summary(validate_project_id(name))
