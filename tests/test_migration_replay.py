"""docs/models-and-storage.md"""
from __future__ import annotations

import io
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.core import files as file_store
from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.models.stage import StageDraft
from app.services import stage_edit
from app.services import project as project_service
from app.models.records.working_copy import WorkingCopy
from app.models.records.methodology import Methodology
from app.models.records.workflow_version import WorkflowVersion
from conftest import queue_added_columns, queue_columns, reads_of

_ALEMBIC_DIRECTORY = Path(__file__).resolve().parents[1] / "alembic"

_CSV = b"id,score\na,1\nb,2\n"
_SOURCE_COLUMNS = [
    {"name": "id", "type": "str", "nullable": True},
    {"name": "score", "type": "int", "nullable": True},
]

_SEEDED_COLLECTIONS = sorted({
    project_service.Project.collection,
    Methodology.collection,
    WorkingCopy.collection,
    WorkflowVersion.collection,
    file_store.ProjectFile.collection,
})


def test_replaying_every_revision_over_a_current_store_rewrites_nothing(tmp_path, monkeypatch):
    db_path = _open_file_backed_store(tmp_path, monkeypatch)
    _seed_store_through_the_app()
    documents, stored_files = _read_documents(db_path), _read_stored_files()
    assert sorted({collection for collection, _, _, _ in documents}) == _SEEDED_COLLECTIONS
    # The state the store is really in: written by this build, met by alembic later.
    assert _read_stamped_revision(db_path) is None

    _upgrade_to_head()

    assert _read_documents(db_path) == documents
    assert _read_stored_files() == stored_files
    assert _read_stamped_revision(db_path) == _head_revision()


def _open_file_backed_store(tmp_path, monkeypatch) -> Path:
    # conftest configures `:memory:`, which alembic's own connection cannot reach.
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(db_path))
    configure_store(SqliteKvStore(str(db_path)))
    return db_path


def _seed_store_through_the_app() -> str:
    project_id = project_service.create_project(
        "replay", "Review every row that scored.", source="migration replay test").id
    upload = file_store.save_upload("rows.csv", io.BytesIO(_CSV), project_id=project_id)
    outcome = project_service.add_stages(stage_edit.open_working_copy(project_id), _stage_drafts(upload))
    assert not outcome.failed and not outcome.batch_issues and not outcome.skipped, outcome
    project_service.save_working_copy_as_version(
        project_id, message="first version")
    return project_id


def _stage_drafts(upload: file_store.ProjectFile) -> list[StageDraft]:
    return [
        StageDraft.model_validate({
            "id": "rows", "description": "Load the scored rows", "type": "input_data",
            "connector": {"kind": "file", "params": {
                "path": str(file_store.resolve_stored_path(upload)), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _SOURCE_COLUMNS},
        }),
        StageDraft.model_validate({
            "id": "review", "description": "Review each row", "type": "human_review_queue",
            "inputs": [{"id": "rows"}],
            "queue": {**queue_columns(), "reviewer_instructions": "Confirm each row."},
            "signature": {"form": "extends", "reads": reads_of("rows", _SOURCE_COLUMNS),
                          "adds": queue_added_columns()},
        }),
    ]


def _read_documents(db_path: Path) -> list[tuple[str, str, str, int]]:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(
            "SELECT collection, id, data, schema_version FROM documents "
            "ORDER BY collection, id").fetchall()
    finally:
        connection.close()


def _read_stored_files() -> dict[str, bytes]:
    root = file_store.files_root()
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _read_stamped_revision(db_path: Path) -> str | None:
    connection = sqlite3.connect(db_path)
    try:
        if not connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone():
            return None
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()
    return None if row is None else str(row[0])


def _upgrade_to_head() -> None:
    command.upgrade(_alembic_config(), "head")


def _head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    assert head is not None, f"no head revision under {_ALEMBIC_DIRECTORY}"
    return head


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIRECTORY))
    return config
