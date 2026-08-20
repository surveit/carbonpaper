"""What the OPERATOR records about a run, beside the run's own manifest: the name
they gave it, and whether the runs index hides it. The manifest itself is the
executor's to write (`app.runtime.manifest`), which is why this is a record of its
own — and why a run keeps every number it reported whatever is written here.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid4

from app.core.persistence import PersistedModel, PersistenceScope
from app.services.workspace import validate_project_id


class RunManifestMetadata(PersistedModel):
    collection: ClassVar[str] = "run_manifest_metadata"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: str
    run_id: str
    archived: bool = False
    # Empty is the unnamed state, which is what a run starts in and what clearing the
    # field returns it to. Never None: nothing distinguishes "never named" from "named
    # and then cleared", and no reader asks.
    name: str = ""


def archive_run(project_id: str, run_id: str) -> None:
    record = _open_record(project_id, run_id)
    record.archived = True
    record.save()


def unarchive_run(project_id: str, run_id: str) -> None:
    record = _open_record(project_id, run_id)
    record.archived = False
    record.save()


def name_run(project_id: str, run_id: str, name: str) -> None:
    """A blank name clears it; the record stays, so an archived run stays archived."""
    record = _open_record(project_id, run_id)
    record.name = name.strip()
    record.save()


def read_run_names(project_id: str) -> dict[str, str]:
    return {r.run_id: r.name for r in read_run_metadata(project_id) if r.name}


def read_run_name(project_id: str, run_id: str) -> str:
    record = _find_record(project_id, run_id)
    return record.name if record else ""


def read_archived_run_ids(project_id: str) -> set[str]:
    return {record.run_id for record in read_run_metadata(project_id) if record.archived}


def read_run_metadata(project_id: str) -> list[RunManifestMetadata]:
    return RunManifestMetadata.list(f"{validate_project_id(project_id)}/")


def _open_record(project_id: str, run_id: str) -> RunManifestMetadata:
    # Reused, not replaced: a rename must not drop an archive flag.
    return _find_record(project_id, run_id) or RunManifestMetadata(
        id=f"{validate_project_id(project_id)}/{uuid4().hex}",
        project_id=project_id,
        run_id=run_id,
    )


def _find_record(project_id: str, run_id: str) -> RunManifestMetadata | None:
    return next(
        (r for r in read_run_metadata(project_id) if r.run_id == run_id), None
    )
