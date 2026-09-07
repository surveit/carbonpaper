"""Seed the stages a test edits — a draft — without the validated writer."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.persistence import get_store
from app.models.records.draft import Draft
from app.models.records.working_copy import WorkingCopy

_STAMP = "2026-01-01T00:00:00"


def add_stage(project: str | Path, spec: dict[str, Any]) -> None:
    """Store one stage spec, creating the working copy if needed."""
    specs = read_stages(project)
    # Upsert by id, the way writing a stage FILE behaved: seeding the same stage
    # twice is an author revising it, not a second stage sharing its id.
    index = next((i for i, s in enumerate(specs) if s.get("id") == spec.get("id")), None)
    if index is None:
        specs.append(spec)
    else:
        specs[index] = spec
    set_stages(project, specs)


# A triplet, because app.services.drafts refuses any other shape as a draft id.
SEED_DRAFT = "seed-draft-fixture"


def set_stages(project: str | Path, specs: list[dict[str, Any]]) -> None:
    """Replace SEED_DRAFT's stages with `specs`; [] stores an empty workflow."""
    name = _name(project)
    get_store().write(Draft.collection, f"{name}/{SEED_DRAFT}", {
        "id": f"{name}/{SEED_DRAFT}", "draft_id": SEED_DRAFT, "parent_version": None,
        "created_at": _STAMP, "updated_at": _STAMP, "stages": list(specs),
    })
    # Readers that have not moved to drafts yet still load the working copy.
    get_store().write(WorkingCopy.collection, name, {
        "id": name, "created_at": _STAMP, "updated_at": _STAMP, "stages": list(specs),
    })


def read_stage(project: str | Path, stage_id: str) -> dict[str, Any]:
    """One stored stage spec by id; KeyError if the draft has no such stage."""
    for spec in read_stages(project):
        if spec.get("id") == stage_id:
            return spec
    raise KeyError(stage_id)


def read_stages(project: str | Path) -> list[dict[str, Any]]:
    key = f"{_name(project)}/{SEED_DRAFT}"
    document = get_store().read_tolerant(Draft.collection, key)
    return list(document["stages"]) if document else []


def _name(project: str | Path) -> str:
    return Path(project).name
