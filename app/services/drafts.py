"""A draft is disposable scratch for a workflow's stages; save_version is its only exit."""
from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, ValidationError

from app.core.errors import DocumentNotFound, DraftNotFoundError
from app.models import (
    Stage,
    parse_stage,
    stage_to_spec_dict,
    validate_workflow,
)
from app.models.records.draft import Draft
from app.core.json_types import JsonDict
from app.core.utils import format_errors, build_word_triplet_id
from app.services import versioning, workspace


class DraftView(BaseModel):
    id: str
    parent_version: str | None
    stages: list[Stage]
    created_at: str
    updated_at: str


class DraftDetail(DraftView):
    issues: list[str]


class DraftEdit(BaseModel):
    ok: bool
    draft_id: str
    stage_ids: list[str]
    issues: list[str]


class SaveResult(BaseModel):
    ok: bool
    issues: list[str] = Field(default_factory=list)
    version_id: str | None = None
    # Separates "someone saved first" from "this workflow is invalid": only one is overridable.
    conflict: bool = False


def create_draft(
    project_id: str,
    *,
    from_version_id: str | None = None,
) -> DraftView:
    project = workspace.validate_project_id(project_id)
    stages = (
        versioning.load_version_stages(project, from_version_id)
        if from_version_id is not None
        else []
    )
    draft_id = build_word_triplet_id(_find_draft_ids_in_use(project))
    d = Draft(
        id=_doc_id(project, draft_id),
        draft_id=draft_id,
        parent_version=from_version_id,
        stages=stages,
    )
    d.save()
    return _build_draft_view(d)


def start_draft_from_newest_version(project_id: str) -> str:
    """A one-shot edit gets a draft of its own rather than sharing anyone else's."""
    project = workspace.validate_project_id(project_id)
    newest = versioning.find_latest_version_id(project)
    return create_draft(project, from_version_id=newest).id


def read_draft(project_id: str, draft_id: str) -> DraftDetail:
    d = _load(workspace.validate_project_id(project_id), draft_id)
    return DraftDetail(**_build_draft_view(d).model_dump(), issues=validate_workflow(d.stages))


def read_draft_stages(project_id: str, draft_id: str) -> dict[str, JsonDict]:
    project = workspace.validate_project_id(project_id)
    return {s.id: stage_to_spec_dict(s) for s in _load(project, draft_id).stages}


def write_draft_stages(project_id: str, draft_id: str, stages: list[JsonDict]) -> None:
    project = workspace.validate_project_id(project_id)
    d = _load(project, draft_id)
    d.stages = [parse_stage(spec) for spec in stages]
    d.save()


def set_draft_stage(project_id: str, draft_id: str, stage_json: str) -> DraftEdit:
    stage = _parse_stage(stage_json)
    d = _load(workspace.validate_project_id(project_id), draft_id)
    kept = [s for s in d.stages if s.id != stage.id]
    d.stages = kept + [stage]
    d.save()
    return _describe(d)


def delete_draft_stage(project_id: str, draft_id: str, stage_id: str) -> DraftEdit:
    d = _load(workspace.validate_project_id(project_id), draft_id)
    kept = [s for s in d.stages if s.id != stage_id]
    if len(kept) == len(d.stages):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    d.stages = kept
    d.save()
    return _describe(d)


def save_version(
    project_id: str, draft_id: str, *, message: str, override_conflict: bool = False
) -> SaveResult:
    project = workspace.validate_project_id(project_id)
    d = _load(project, draft_id)
    issues = validate_workflow(d.stages)
    if issues:
        return SaveResult(ok=False, issues=issues)
    lost = _find_lost_version(project, d)
    if lost is not None and not override_conflict:
        return SaveResult(ok=False, conflict=True, issues=[
            f"this draft was started from {d.parent_version}, and {lost} has been "
            f"saved since — saving now writes a version carrying none of its changes. "
            f"Read it, or pass override_conflict to save anyway."
        ])
    meta = versioning.create_version_from_stages(
        project,
        [stage_to_spec_dict(s) for s in d.stages],
        message=message,
        parent_version=d.parent_version,
    )
    d.parent_version = meta.version_id
    d.save()
    return SaveResult(ok=True, version_id=meta.version_id)


# ─── internals ───────────────────────────────────────────────────────────────


def _build_draft_view(d: Draft) -> DraftView:
    return DraftView(
        id=d.draft_id,
        parent_version=d.parent_version,
        stages=d.stages,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def _find_lost_version(project_id: str, d: Draft) -> str | None:
    """A draft that never claimed a base cannot have lost anything."""
    if d.parent_version is None:
        return None
    newest = versioning.find_latest_version_id(project_id)
    return newest if newest is not None and newest != d.parent_version else None


_DRAFT_ID = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def _doc_id(project_id: str, draft_id: str) -> str:
    return f"{project_id}/{draft_id}"


def _find_draft_ids_in_use(project_id: str) -> set[str]:
    return {d.draft_id for d in Draft.list(f"{project_id}/")}


def _load(project_id: str, draft_id: str) -> Draft:
    """Shape-checks the id FIRST, so a caller-supplied id can never reach the store as a key."""
    if not _DRAFT_ID.match(draft_id):
        raise DraftNotFoundError(f"'{draft_id}' is not a draft id")
    try:
        return Draft.load(_doc_id(project_id, draft_id))
    except DocumentNotFound as exc:
        raise DraftNotFoundError(
            f"No draft '{draft_id}' for project '{project_id}' — drafts are "
            f"disposable; start a new one with create_draft."
        ) from exc


def _parse_stage(stage_json: str) -> Stage:
    obj = json.loads(stage_json)
    if not isinstance(obj, dict):
        raise ValueError("stage_json must be a JSON object")
    try:
        return parse_stage(obj)
    except ValidationError as exc:
        raise ValueError("; ".join(format_errors(exc))) from exc


def _describe(d: Draft) -> DraftEdit:
    return DraftEdit(
        ok=True,
        draft_id=d.draft_id,
        stage_ids=[s.id for s in d.stages],
        issues=validate_workflow(d.stages),
    )
