"""Seed the stages a test edits — a draft and a version — without the validated writer."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.persistence import get_store
from app.core.timestamp_ids import mint_timestamp_id
from app.models.records.draft import Draft
from app.models.stage import Stage, stage_to_spec_dict
from app.models.records.workflow_version import WorkflowVersion

_STAMP = "2026-01-01T00:00:00"

# The message marking the version this file wrote, so re-seeding replaces only its own.
SEED_VERSION_MESSAGE = "stage_seed fixture stages"


def add_stage(project: str | Path, spec: dict[str, Any]) -> None:
    """Store one stage spec, creating the draft if needed."""
    specs = read_stages(project)
    # Upsert by id, the way writing a stage FILE behaved: seeding the same stage
    # twice is an author revising it, not a second stage sharing its id.
    index = next((i for i, s in enumerate(specs) if s.get("id") == spec.get("id")), None)
    if index is None:
        specs.append(spec)
    else:
        specs[index] = spec
    _store_stages(project, specs)


# A triplet, because app.services.drafts refuses any other shape as a draft id.
SEED_DRAFT = "seed-draft-fixture"


def _store_stages(project: str | Path, specs: list[dict[str, Any]]) -> None:
    """Replace SEED_DRAFT's stages with `specs`; [] stores an empty workflow."""
    name = _name(project)
    get_store().write(Draft.collection, f"{name}/{SEED_DRAFT}", {
        "id": f"{name}/{SEED_DRAFT}", "draft_id": SEED_DRAFT, "parent_version": None,
        "created_at": _STAMP, "updated_at": _STAMP, "stages": list(specs),
    })
    _write_seed_version(name, specs)


def _write_seed_version(name: str, specs: list[dict[str, Any]]) -> None:
    # Straight through the store, because tests seed specs a validated writer refuses.
    _drop_seed_versions(name)
    version_id = mint_timestamp_id()
    get_store().write(WorkflowVersion.collection, f"{name}/{version_id}", {
        "id": f"{name}/{version_id}", "created_at": _STAMP, "updated_at": _STAMP,
        "version_id": version_id, "message": SEED_VERSION_MESSAGE,
        "stages": list(specs), "schemas": [],
    }, schema_version=WorkflowVersion.SCHEMA_VERSION)


def _drop_seed_versions(name: str) -> None:
    stale = [doc_id for doc_id, data in get_store().read_all(
        WorkflowVersion.collection, f"{name}/")
        if data.get("message") == SEED_VERSION_MESSAGE]
    for doc_id in stale:
        get_store().delete(WorkflowVersion.collection, doc_id)


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


def set_stages(project: str | Path, specs: list[dict[str, Any]]) -> None:
    """Seed the draft and the version a test reads back."""
    _store_stages(project, specs)


def set_parsed_stages(project: str | Path, stages: list[Stage]) -> None:
    set_stages(project, [stage_to_spec_dict(stage) for stage in stages])


def save_version(
    project: str | Path, *, message: str, parent_version: str | None = None
) -> WorkflowVersion:
    """Mint a real version from the seeded stages, retiring the seed's own version."""
    from app.services import versioning

    name = _name(project)
    version = versioning.create_version_from_stages(
        name, read_stages(project), message=message, parent_version=parent_version
    )
    _drop_seed_versions(name)
    return version


def drop_versions(project: str | Path) -> None:
    """Leave the project with no stages at all: a version is the only place they live."""
    for doc_id in WorkflowVersion.list_ids(f"{_name(project)}/"):
        get_store().delete(WorkflowVersion.collection, doc_id)


def list_saved_version_ids(project: str | Path) -> list[str]:
    """The version ids minted by the code under test, never the seed's own."""
    return [str(data["version_id"]) for _, data in get_store().read_all(
        WorkflowVersion.collection, f"{_name(project)}/")
        if data.get("message") != SEED_VERSION_MESSAGE]


def seed_version(project: str | Path, specs: list[dict[str, Any]]) -> str:
    _store_stages(project, specs)
    return save_version(project, message="seeded").version_id


def start_draft(project: str | Path) -> str:
    """An empty SEED_DRAFT, for a test whose first stage write goes through a tool."""
    _store_stages(project, [])
    return SEED_DRAFT
