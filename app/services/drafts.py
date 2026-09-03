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
from app.services import stage_edit, versioning, workspace


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


def _build_draft_view(d: Draft) -> DraftView:
    return DraftView(
        id=d.draft_id,
        parent_version=d.parent_version,
        stages=d.stages,
        created_at=d.created_at,
        updated_at=d.updated_at,
    )


def create_draft(
    name: str,
    *,
    from_version: str | None = None,
) -> DraftView:
    project = workspace.validate_project_id(name)
    stages = (
        versioning.load_version_stages(project, from_version)
        if from_version is not None
        else []
    )
    draft_id = build_word_triplet_id(_find_draft_ids_in_use(project))
    d = Draft(
        id=_doc_id(project, draft_id),
        draft_id=draft_id,
        parent_version=from_version,
        stages=stages,
    )
    d.save()
    return _build_draft_view(d)


def open_session_draft(name: str, session_id: str) -> DraftView:
    """Idempotent: a session's first edit seeds its draft from the newest version."""
    project = workspace.validate_project_id(name)
    try:
        return _build_draft_view(_load(project, session_id))
    except DraftNotFoundError:
        pass
    parent = versioning.find_latest_version_id(project)
    d = Draft(
        id=_doc_id(project, session_id),
        draft_id=session_id,
        parent_version=parent,
        stages=versioning.load_version_stages(project, parent) if parent else [],
    )
    d.save()
    return _build_draft_view(d)


def read_draft(
    name: str, draft_id: str) -> DraftDetail:
    d = _load(workspace.validate_project_id(name), draft_id)
    return DraftDetail(**_build_draft_view(d).model_dump(), issues=validate_workflow(d.stages))


def set_draft_stage(
    name: str, draft_id: str, stage_json: str) -> DraftEdit:
    stage = _parse_stage(stage_json)
    d = _load(workspace.validate_project_id(name), draft_id)
    kept = [s for s in d.stages if s.id != stage.id]
    d.stages = kept + [stage]
    d.save()
    return _describe(d)


def delete_draft_stage(
    name: str, draft_id: str, stage_id: str) -> DraftEdit:
    d = _load(workspace.validate_project_id(name), draft_id)
    kept = [s for s in d.stages if s.id != stage_id]
    if len(kept) == len(d.stages):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    d.stages = kept
    d.save()
    return _describe(d)


def save_version(
    name: str, draft_id: str, *, message: str, override_conflict: bool = False
) -> SaveResult:
    project = workspace.validate_project_id(name)
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


def open_session_stages(name: str, session_id: str) -> stage_edit.StageSpecStore:
    """The store an edit made in this session reads and writes, seeding the draft if new."""
    project = workspace.validate_project_id(name)
    open_session_draft(project, session_id)

    def read() -> dict[str, JsonDict]:
        return {s.id: stage_to_spec_dict(s) for s in _load(project, session_id).stages}

    def write(specs: list[JsonDict]) -> None:
        d = _load(project, session_id)
        d.stages = [parse_stage(spec) for spec in specs]
        d.save()

    return stage_edit.StageSpecStore(project_id=project, read=read, write=write)


# ─── internals ───────────────────────────────────────────────────────────────


def _find_lost_version(project_id: str, d: Draft) -> str | None:
    """A draft that never claimed a base cannot have lost anything."""
    if d.parent_version is None:
        return None
    newest = versioning.find_latest_version_id(project_id)
    return newest if newest is not None and newest != d.parent_version else None

# A word triplet names a draft someone started; 32 hex names a session's own.
_DRAFT_ID = re.compile(r"^([a-z]+-[a-z]+-[a-z]+|[0-9a-f]{32})$")


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
