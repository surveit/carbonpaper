"""Seed a project's stored working copy directly, for tests.

Production writes go through the validated writer (`app.services.stage_edit`);
these do not, so a test can store the exact spec it means to — including one
that no longer parses, which is the case the tolerant loader exists for.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.persistence import get_store
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


def set_stages(project: str | Path, specs: list[dict[str, Any]]) -> None:
    """Replace the working copy with `specs`; [] stores an empty workflow."""
    name = _name(project)
    get_store().write(WorkingCopy.collection, name, {
        "id": name, "created_at": _STAMP, "updated_at": _STAMP, "stages": list(specs),
    })


def read_stage(project: str | Path, stage_id: str) -> dict[str, Any]:
    """One stored stage spec by id; KeyError if the working copy has no such stage."""
    for spec in read_stages(project):
        if spec.get("id") == stage_id:
            return spec
    raise KeyError(stage_id)


def read_stages(project: str | Path) -> list[dict[str, Any]]:
    document = get_store().read_tolerant(WorkingCopy.collection, _name(project))
    return list(document["stages"]) if document else []


def _name(project: str | Path) -> str:
    return Path(project).name
