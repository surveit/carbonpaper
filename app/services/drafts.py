"""drafts.py — the DRAFT lifecycle: disposable, mutable scratch space for a
workflow's stages.

A draft is where an agent (or, later, a UI edit buffer) assembles stage specs
before freezing them into an immutable version. Unlike a version it is mutable,
allowed to be INVALID mid-edit, and carries no promise of survival: the files
live at <project>/drafts/<draft_id>.json purely so an in-flight edit survives a
server restart — anything may delete them at any time, nothing may depend on a
draft existing, and drafts are never project state (not listed in the UI, not
versioned, not run). The only exit is save_version, which strict-validates and
refuses rather than persist an invalid workflow.

Draft ids are word triplets (e.g. brisk-otter-lamp): short enough for an agent
to retype reliably, and unmistakable for a timestamp version id."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, NotRequired, TypedDict, cast

from app.core.errors import DraftNotFoundError
from app.core.models.workflow import validate_workflow_draft
from app.core.utils import generate_word_triplet_id
from app.services import versioning, workspace
from app.services.loader import stage_to_spec_dict


class Draft(TypedDict):
    """The <project>/drafts/<draft_id>.json record: mutable scratch stages an
    agent assembles before freezing them into an immutable version."""
    id: str
    parent_version: str | None
    stages: list[dict[str, Any]]   # raw stage specs — the JSON boundary, may be invalid mid-edit
    created_at: str
    updated_at: str


class DraftView(Draft):
    """A draft plus its current, non-fatal validation problems."""
    issues: list[str]              # non-fatal validation problems in the current stages


class DraftEditResult(TypedDict):
    """The summary every draft edit (set/remove stage) returns."""
    ok: bool
    draft_id: str
    stage_ids: list[str | None]
    issues: list[str]


class SaveResult(TypedDict):
    """The outcome of freezing a draft into a version: the new version's meta on
    success, or the blocking issues on failure."""
    ok: bool
    issues: NotRequired[list[str]]
    version: NotRequired[dict[str, Any]]   # a WorkflowVersion.model_dump(mode="json") at the agent JSON boundary


def create_draft(
    name: str,
    *,
    from_version: str | None = None,
    examples_dir: Path | None = None,
) -> Draft:
    """Start a new draft for a project and return it (its `id` names it in every
    later call). Seeded with the stages of `from_version` when given (and
    recording it as the draft's parent), empty otherwise."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    if from_version is not None:
        stages = [
            stage_to_spec_dict(stage)
            for stage in versioning.load_version_stages(project_dir, from_version)
        ]
    else:
        stages = []
    directory = _drafts_dir(project_dir)
    directory.mkdir(parents=True, exist_ok=True)
    taken = {path.stem for path in directory.glob("*.json")}
    now = datetime.now().isoformat(timespec="seconds")
    draft: Draft = {
        "id": generate_word_triplet_id(taken),
        "parent_version": from_version,
        "stages": stages,
        "created_at": now,
        "updated_at": now,
    }
    _write_draft(project_dir, draft)
    return draft


def read_draft(
    name: str, draft_id: str, *, examples_dir: Path | None = None
) -> DraftView:
    """The draft plus a non-fatal `issues` list (schema + graph problems in its
    current stages; [] means it would save cleanly)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    draft = _load_draft(project_dir, draft_id)
    return {**draft, "issues": validate_workflow_draft(draft["stages"])}


def set_draft_stage(
    name: str, draft_id: str, stage_json: str, *, examples_dir: Path | None = None
) -> DraftEditResult:
    """Add or replace one stage (matched by its `id`) in the draft. The stage is
    stored even when the resulting workflow is invalid — a draft mid-surgery may
    have dangling edges — and the current problems come back as `issues` so the
    caller always sees them. Raises ValueError for JSON that is not a stage
    object with a string `id`."""
    stage = _parse_stage_object(stage_json)
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    draft = _load_draft(project_dir, draft_id)
    kept = [s for s in draft["stages"] if s.get("id") != stage["id"]]
    draft["stages"] = kept + [stage]
    _write_draft(project_dir, draft)
    return _describe(draft)


def remove_draft_stage(
    name: str, draft_id: str, stage_id: str, *, examples_dir: Path | None = None
) -> DraftEditResult:
    """Delete one stage from the draft by id. Raises ValueError if no stage in
    the draft carries that id (deleting nothing is a caller mistake, not a
    success)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    draft = _load_draft(project_dir, draft_id)
    kept = [s for s in draft["stages"] if s.get("id") != stage_id]
    if len(kept) == len(draft["stages"]):
        raise ValueError(f"No stage '{stage_id}' in draft '{draft_id}'")
    draft["stages"] = kept
    _write_draft(project_dir, draft)
    return _describe(draft)


def save_version(
    name: str, draft_id: str, *, message: str, examples_dir: Path | None = None
) -> SaveResult:
    """Freeze the draft's stages into a new immutable version — the draft's only
    exit, and the single validation cliff: an invalid draft is refused with the
    full issue list and nothing is written. On success the draft's parent
    advances to the new version, so successive saves chain (v2 -> v3 -> v4)
    rather than fanning out; the version is born unpublished (publishing is the
    human's act)."""
    project_dir = workspace.resolve_project_dir(name, examples_dir)
    draft = _load_draft(project_dir, draft_id)
    issues = validate_workflow_draft(draft["stages"])
    if issues:
        return {"ok": False, "issues": issues}
    meta = versioning.create_version_from_stages(
        project_dir,
        draft["stages"],
        message=message,
        reviewer="agent",
        parent_version=draft["parent_version"],
    )
    draft["parent_version"] = meta.id
    _write_draft(project_dir, draft)
    return {"ok": True, "version": meta.model_dump(mode="json")}


# ─── internals ───────────────────────────────────────────────────────────────

_DRAFT_ID = re.compile(r"^[a-z]+-[a-z]+-[a-z]+$")


def _drafts_dir(project_dir: Path) -> Path:
    return Path(project_dir) / "drafts"


def _draft_path(project_dir: Path, draft_id: str) -> Path:
    """The draft's file, with the id checked against the triplet shape first so a
    caller-supplied id can never escape the drafts directory."""
    if not _DRAFT_ID.match(draft_id):
        raise DraftNotFoundError(f"'{draft_id}' is not a draft id")
    return _drafts_dir(project_dir) / f"{draft_id}.json"


def _load_draft(project_dir: Path, draft_id: str) -> Draft:
    path = _draft_path(project_dir, draft_id)
    if not path.is_file():
        raise DraftNotFoundError(
            f"No draft '{draft_id}' for project '{project_dir.name}' — drafts are "
            f"disposable; start a new one with create_draft."
        )
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise DraftNotFoundError(
            f"Draft file for '{draft_id}' is corrupt (not a JSON object) — "
            f"start a new draft with create_draft."
        )
    # Trusted as a Draft: every write goes through _write_draft, which only ever
    # writes the shape create_draft/set_draft_stage/remove_draft_stage build.
    return cast(Draft, loaded)


def _write_draft(project_dir: Path, draft: Draft) -> None:
    draft["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = _draft_path(project_dir, str(draft["id"]))
    path.write_text(json.dumps(draft, indent=2), encoding="utf-8")


def _parse_stage_object(stage_json: str) -> dict[str, Any]:
    stage = json.loads(stage_json)
    if not isinstance(stage, dict) or not isinstance(stage.get("id"), str):
        raise ValueError("stage_json must be a JSON object with a string 'id'")
    return stage


def _describe(draft: Draft) -> DraftEditResult:
    """The summary every edit returns: what's in the draft now, and whether it
    would save cleanly."""
    return {
        "ok": True,
        "draft_id": draft["id"],
        "stage_ids": [s.get("id") for s in draft["stages"]],
        "issues": validate_workflow_draft(draft["stages"]),
    }
