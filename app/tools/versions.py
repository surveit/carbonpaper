"""The tools that read a SAVED version — what everyone but the editor of a draft sees."""

from __future__ import annotations

from pydantic import BaseModel

from app.models.stage import stage_to_json
from app.services import versioning, workspace


class VersionListing(BaseModel):
    version_id: str
    created_at: str
    message: str
    stages: list[str]


def list_versions(project_id: str) -> list[VersionListing]:
    project = workspace.validate_project_id(project_id)
    return [
        VersionListing(
            version_id=v.version_id,
            created_at=v.created_at,
            message=v.message,
            stages=[stage.id for stage in v.stages],
        )
        for v in versioning.list_versions(project)
    ]


def read_version_stage(project_id: str, version_id: str, stage_id: str) -> str:
    project = workspace.validate_project_id(project_id)
    for stage in versioning.load_version_stages(project, version_id):
        if stage.id == stage_id:
            return stage_to_json(stage)
    raise ValueError(f"no stage '{stage_id}' in version '{version_id}'")
