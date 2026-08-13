"""The DRAFT lifecycle: disposable, mutable scratch space for a workflow's stages.

Anything may delete a draft at any time; nothing may depend on one existing. Every
stored stage is individually valid, but WORKFLOW-level incompleteness (dangling
`inputs`, duplicate ids, cycles) stays allowed until save_version, the only exit."""
from __future__ import annotations

import json
import re
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.core.errors import DocumentNotFound, DraftNotFoundError
from app.models import (
    STAGE_SPEC_SCHEMA_VERSION,
    Stage,
    parse_stage,
    stage_to_spec_dict,
    validate_workflow,
)
from app.core.persistence import PersistedModel, PersistenceScope
from app.core.utils import format_errors, generate_word_triplet_id
from app.services import versioning, workspace


class Draft(PersistedModel):
    """`id` is the composite `f"{project_id}/{draft_id}"`; `draft_id` is the local id callers use."""

    collection: ClassVar[str] = "draft"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ
    SCHEMA_VERSION: ClassVar[int] = STAGE_SPEC_SCHEMA_VERSION
    # Dump embedded stages in their spec-dict shape (field aliases
    # restored, unset optionals dropped) — mirrors WorkflowVersion.DUMP_OPTS,
    # so a draft's on-disk stage shape matches a version's.
    DUMP_OPTS: ClassVar[dict[str, Any]] = {"by_alias": True, "exclude_none": True}

    draft_id: str
    parent_version: str | None = None
    stages: list[Stage] = Field(default_factory=list)


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


def _view(d: Draft) -> DraftView:
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
    draft_id = generate_word_triplet_id(_taken(project))
    d = Draft(
        id=_doc_id(project, draft_id),
        draft_id=draft_id,
        parent_version=from_version,
        stages=stages,
    )
    d.save()
    return _view(d)


def read_draft(
    name: str, draft_id: str) -> DraftDetail:
    d = _load(workspace.validate_project_id(name), draft_id)
    return DraftDetail(**_view(d).model_dump(), issues=validate_workflow(d.stages))


def set_draft_stage(
    name: str, draft_id: str, stage_json: str) -> DraftEdit:
    stage = _parse_stage(stage_json)
    d = _load(workspace.validate_project_id(name), draft_id)
    kept = [s for s in d.stages if s.id != stage.id]
    d.stages = kept + [stage]
    d.save()
    return _describe(d)


def remove_draft_stage(
    name: str, draft_id: str, stage_id: str) -> DraftEdit:
    d = _load(workspace.validate_project_id(name), draft_id)
    kept = [s for s in d.stages if s.id != stage_id]
    if len(kept) == len(d.stages):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    d.stages = kept
    d.save()
    return _describe(d)


def save_version(
    name: str, draft_id: str, *, message: str
) -> SaveResult:
    project = workspace.validate_project_id(name)
    d = _load(project, draft_id)
    issues = validate_workflow(d.stages)
    if issues:
        return SaveResult(ok=False, issues=issues)
    meta = versioning.create_version_from_stages(
        project,
        [stage_to_spec_dict(s) for s in d.stages],
        message=message,
        reviewer="agent",
        parent_version=d.parent_version,
    )
    d.parent_version = meta.version_id
    d.save()
    return SaveResult(ok=True, version_id=meta.version_id)


# ─── internals ───────────────────────────────────────────────────────────────

_DRAFT_ID = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def _doc_id(project_id: str, draft_id: str) -> str:
    return f"{project_id}/{draft_id}"


def _taken(project_id: str) -> set[str]:
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
