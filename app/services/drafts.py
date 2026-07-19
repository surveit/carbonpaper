"""drafts.py — the DRAFT lifecycle: disposable, mutable scratch space for a
workflow's stages.

A draft is where an agent (or, later, a UI edit buffer) assembles stage specs
before freezing them into an immutable version. Unlike a version it is mutable,
allowed to be INVALID mid-edit, and carries no promise of survival: a `Draft`
is a document in the store's "draft" collection, doc id `f"{project}/{draft_id}"`
— project-scoped like every other collection — kept purely so an in-flight edit
survives a server restart. Anything may delete a draft at any time, nothing may
depend on one existing, and drafts are never project state (not versioned, not
run). The only exit is save_version, which strict-validates and refuses rather
than persist an invalid workflow.

Draft ids are word triplets (e.g. brisk-otter-lamp): short enough for an agent
to retype reliably, and unmistakable for a timestamp version id — see
app.core.utils.generate_word_triplet_id.

Dependency note: this module may import app.services.versioning (to seed from
and freeze into a version) and app.services.workspace (to resolve a project
name to its directory), and app.core.*, but nothing from app.runtime or
app.compiler."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from pydantic import Field

from app.core.errors import DocumentNotFound, DraftNotFoundError
from app.core.models.workflow import validate_workflow_draft
from app.core.persistence import PersistedModel
from app.core.utils import generate_word_triplet_id
from app.services import versioning, workspace
from app.services.loader import stage_to_spec_dict


class Draft(PersistedModel):
    """One scratch document in the "draft" collection. `id` (inherited from
    PersistedModel) is the composite `f"{project}/{draft_id}"`; `draft_id` is
    the plain local id every caller of this module's public functions works
    with. `stages` are RAW stage spec dicts — the JSON boundary, may be invalid
    mid-edit — never typed `Stage` objects, since a draft is allowed to be
    unloadable as a workflow until save_version."""

    collection: ClassVar[str] = "draft"

    draft_id: str
    parent_version: str | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


def _view(d: Draft) -> dict[str, Any]:
    """The agent-facing shape every caller of this module reads: `id` is the
    LOCAL draft_id, never the composite store id — mirrors versioning's
    `_meta`."""
    return {
        "id": d.draft_id,
        "parent_version": d.parent_version,
        "stages": d.stages,
        "created_at": d.created_at,
        "updated_at": d.updated_at,
    }


def create_draft(
    name: str,
    *,
    from_version: str | None = None,
    examples_dir: Path | None = None,
) -> dict[str, Any]:
    """Start a new draft for project `name` and return its view (its `id` names
    it in every later call). Seeded with the stages of `from_version` when
    given (and recording it as the draft's parent), empty otherwise."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    if from_version is not None:
        stages = [
            stage_to_spec_dict(stage)
            for stage in versioning.load_version_stages(project_dir, from_version)
        ]
    else:
        stages = []
    draft_id = generate_word_triplet_id(_taken(project_dir))
    now = datetime.now().isoformat(timespec="seconds")
    d = Draft(
        id=_doc_id(project_dir, draft_id),
        draft_id=draft_id,
        parent_version=from_version,
        stages=stages,
        created_at=now,
        updated_at=now,
    )
    d.save()
    return _view(d)


def read_draft(
    name: str, draft_id: str, *, examples_dir: Path | None = None
) -> dict[str, Any]:
    """The draft's view plus a non-fatal `issues` list (schema + graph problems
    in its current stages; [] means it would save cleanly)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    return {**_view(d), "issues": validate_workflow_draft(d.stages)}


def set_draft_stage(
    name: str, draft_id: str, stage_json: str, *, examples_dir: Path | None = None
) -> dict[str, Any]:
    """Add or replace one stage (matched by its `id`) in the draft. The stage is
    stored even when the resulting workflow is invalid — a draft mid-surgery may
    have dangling edges — and the current problems come back as `issues` so the
    caller always sees them. Raises ValueError for JSON that is not a stage
    object with a string `id`."""
    stage = _parse_stage_object(stage_json)
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    kept = [s for s in d.stages if s.get("id") != stage["id"]]
    d.stages = kept + [stage]
    _save(d)
    return _describe(d)


def remove_draft_stage(
    name: str, draft_id: str, stage_id: str, *, examples_dir: Path | None = None
) -> dict[str, Any]:
    """Delete one stage from the draft by id. Raises ValueError if no stage in
    the draft carries that id (deleting nothing is a caller mistake, not a
    success)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    kept = [s for s in d.stages if s.get("id") != stage_id]
    if len(kept) == len(d.stages):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    d.stages = kept
    _save(d)
    return _describe(d)


def save_version(
    name: str, draft_id: str, *, message: str, examples_dir: Path | None = None
) -> dict[str, Any]:
    """Freeze the draft's stages into a new immutable version — the draft's only
    exit, and the single validation cliff: an invalid draft is refused with the
    full issue list and nothing is written. On success the draft's parent
    advances to the new version, so successive saves chain (v2 -> v3 -> v4)
    rather than fanning out; the version is born unpublished (publishing is the
    human's act)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    d = _load(project_dir, draft_id)
    issues = validate_workflow_draft(d.stages)
    if issues:
        return {"ok": False, "issues": issues}
    meta = versioning.create_version_from_stages(
        project_dir,
        d.stages,
        message=message,
        reviewer="agent",
        parent_version=d.parent_version,
    )
    d.parent_version = meta["id"]
    _save(d)
    return {"ok": True, "version": meta}


# ─── internals ───────────────────────────────────────────────────────────────

_DRAFT_ID = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def _doc_id(project_dir: Path, draft_id: str) -> str:
    return f"{Path(project_dir).name}/{draft_id}"


def _taken(project_dir: Path) -> set[str]:
    """The local draft ids already live for this project — the space
    generate_word_triplet_id must avoid."""
    return {d.draft_id for d in Draft.list(f"{Path(project_dir).name}/")}


def _load(project_dir: Path, draft_id: str) -> Draft:
    """The draft, with the id checked against the triplet shape FIRST so a
    caller-supplied id can never be used as a store key it wasn't minted from
    (e.g. a path-traversal attempt), then loaded from the store. A missing
    document reads the same as a malformed id — both mean "no such draft"."""
    if not _DRAFT_ID.match(draft_id):
        raise DraftNotFoundError(f"'{draft_id}' is not a draft id")
    try:
        return Draft.load(_doc_id(project_dir, draft_id))
    except DocumentNotFound as exc:
        raise DraftNotFoundError(
            f"No draft '{draft_id}' for project '{Path(project_dir).name}' — drafts are "
            f"disposable; start a new one with create_draft."
        ) from exc


def _save(d: Draft) -> None:
    d.updated_at = datetime.now().isoformat(timespec="seconds")
    d.save()


def _parse_stage_object(stage_json: str) -> dict[str, Any]:
    stage = json.loads(stage_json)
    if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
        raise ValueError("stage_json must be a JSON object with a string 'id'")
    return stage


def _describe(d: Draft) -> dict[str, Any]:
    """The summary every edit returns: what's in the draft now, and whether it
    would save cleanly."""
    return {
        "ok": True,
        "draft_id": d.draft_id,
        "stage_ids": [s.get("id") for s in d.stages],
        "issues": validate_workflow_draft(d.stages),
    }
