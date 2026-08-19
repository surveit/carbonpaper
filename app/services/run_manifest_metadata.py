"""What the OPERATOR records about a run, beside the run's own manifest: today,
whether the runs index hides it. The manifest itself is the executor's to write
(`app.runtime.manifest`), which is why this is a record of its own — and why a run
keeps every number it reported whatever is written here.
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


def archive_run(project_id: str, run_id: str) -> None:
    _record_archived(project_id, run_id, archived=True)


def unarchive_run(project_id: str, run_id: str) -> None:
    _record_archived(project_id, run_id, archived=False)


def read_archived_run_ids(project_id: str) -> set[str]:
    return {record.run_id for record in read_run_metadata(project_id) if record.archived}


def read_run_metadata(project_id: str) -> list[RunManifestMetadata]:
    return RunManifestMetadata.list(f"{validate_project_id(project_id)}/")


def _record_archived(project_id: str, run_id: str, *, archived: bool) -> None:
    # Edited in place, so whatever else the record holds survives a trip through
    # the archive and back.
    record = _find_record(project_id, run_id)
    if record is None:
        record = RunManifestMetadata(
            id=f"{validate_project_id(project_id)}/{uuid4().hex}",
            project_id=project_id,
            run_id=run_id,
        )
    record.archived = archived
    record.save()


def _find_record(project_id: str, run_id: str) -> RunManifestMetadata | None:
    return next(
        (r for r in read_run_metadata(project_id) if r.run_id == run_id), None
    )
