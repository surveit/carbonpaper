"""Which of a project's runs the runs index hides. Archiving states nothing about
the run — a run's own record is the executor's to write — so it is a record of its
own, and tidying the list leaves every number the run reported where it was.
"""

from __future__ import annotations

from typing import ClassVar
from uuid import uuid4

from app.core.persistence import PersistedModel, PersistenceScope
from app.services.workspace import validate_project_id


class ArchivedRun(PersistedModel):
    collection: ClassVar[str] = "archived_run"
    SCOPE: ClassVar[PersistenceScope] = PersistenceScope.PROJECT_READ

    project_id: str
    run_id: str


def archive_run(project_id: str, run_id: str) -> None:
    """Idempotent: a run already archived stays archived under its first record."""
    if run_id in read_archived_run_ids(project_id):
        return
    ArchivedRun(
        id=f"{validate_project_id(project_id)}/{uuid4().hex}",
        project_id=project_id,
        run_id=run_id,
    ).save()


def unarchive_run(project_id: str, run_id: str) -> None:
    for record in _read_records(project_id):
        if record.run_id == run_id:
            ArchivedRun.delete(record.id)


def read_archived_run_ids(project_id: str) -> set[str]:
    return {record.run_id for record in _read_records(project_id)}


def _read_records(project_id: str) -> list[ArchivedRun]:
    return ArchivedRun.list(f"{validate_project_id(project_id)}/")
