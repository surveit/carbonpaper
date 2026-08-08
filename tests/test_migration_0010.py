"""0010 backfills a project's identity record from the project.json that used to be
the second store — and refuses every project whose two sides do not agree."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from app.services.project import Project

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0010_backfill_project_records.py")
_CREATE_DOCUMENTS = (
    "CREATE TABLE documents ("
    "  collection TEXT NOT NULL,"
    "  id TEXT NOT NULL,"
    "  data TEXT NOT NULL,"
    "  schema_version INTEGER NOT NULL DEFAULT 1,"
    "  PRIMARY KEY (collection, id))"
)


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0010", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def connection() -> Any:
    conn = create_engine("sqlite://").connect()
    conn.exec_driver_sql(_CREATE_DOCUMENTS)
    yield conn
    conn.close()


def _write_project_file(root: Path, name: str, body: str) -> None:
    (root / name).mkdir(parents=True, exist_ok=True)
    (root / name / "project.json").write_text(body, encoding="utf-8")


def _stored(connection: Connection, name: str) -> dict[str, Any] | None:
    row = connection.exec_driver_sql(
        "SELECT data FROM documents WHERE collection='project' AND id=?", (name,)
    ).fetchone()
    return None if row is None else json.loads(row[0])


def _record_count(connection: Connection) -> int:
    row = connection.exec_driver_sql(
        "SELECT count(*) FROM documents WHERE collection='project'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert(connection: Connection, record: dict[str, Any]) -> None:
    connection.exec_driver_sql(
        "INSERT INTO documents (collection, id, data, schema_version) VALUES (?, ?, ?, 1)",
        ("project", record["id"], json.dumps(record)),
    )


def _record(name: str, **fields: Any) -> dict[str, Any]:
    return {"id": name, "created_at": "2026-01-01T00:00:00.000000",
            "updated_at": "2026-01-01T00:00:00.000000", "title": None, "model": None,
            "source": None, "authored_at": None, **fields}


def test_a_project_file_with_no_record_gets_one(tmp_path, connection):
    _write_project_file(tmp_path, "congresswatch", json.dumps(
        {"name": "congresswatch", "title": None, "created_at": "2026-03-04T09:00:00",
         "model": "sonnet", "source": "pasted document"}))

    assert _load_revision().backfill_project_records(connection, tmp_path) == []

    stored = _stored(connection, "congresswatch")
    assert stored is not None
    # The project's OWN date lands on authored_at; created_at stamps the record.
    assert stored["authored_at"] == "2026-03-04T09:00:00"
    assert (stored["model"], stored["source"]) == ("sonnet", "pasted document")
    assert stored["created_at"] != stored["authored_at"]


def test_the_backfilled_record_satisfies_todays_project_model(tmp_path, connection):
    _write_project_file(tmp_path, "demo", json.dumps(
        {"name": "demo", "model": "sonnet", "source": "seed"}))
    _load_revision().backfill_project_records(connection, tmp_path)

    project = Project.model_validate(_stored(connection, "demo"))

    assert (project.id, project.model, project.authored_at) == ("demo", "sonnet", None)


def test_an_agreeing_record_is_left_exactly_as_it_was(tmp_path, connection):
    _insert(connection, _record("drift", model="sonnet", authored_at="2026-02-02T10:00:00"))
    _write_project_file(tmp_path, "drift", json.dumps(
        {"name": "drift", "model": "sonnet", "created_at": "2026-02-02T10:00:00"}))

    assert _load_revision().backfill_project_records(connection, tmp_path) == []

    assert _stored(connection, "drift") == _record(
        "drift", model="sonnet", authored_at="2026-02-02T10:00:00")
    assert _record_count(connection) == 1


def test_running_it_twice_neither_duplicates_nor_rewrites(tmp_path, connection):
    _write_project_file(tmp_path, "lobbymap", json.dumps(
        {"name": "lobbymap", "model": "sonnet", "created_at": "2026-02-02T10:00:00"}))
    revision = _load_revision()

    revision.backfill_project_records(connection, tmp_path)
    first = _stored(connection, "lobbymap")
    assert revision.backfill_project_records(connection, tmp_path) == []

    assert _record_count(connection) == 1
    assert _stored(connection, "lobbymap") == first


def test_a_disagreeing_record_is_refused_and_left_untouched(tmp_path, connection):
    _insert(connection, _record("drift", model="sonnet"))
    _write_project_file(tmp_path, "drift", json.dumps({"name": "drift", "model": "opus"}))

    [refusal] = _load_revision().backfill_project_records(connection, tmp_path)

    assert refusal.startswith("drift: model:")
    assert "'opus'" in refusal and "'sonnet'" in refusal
    assert _stored(connection, "drift") == _record("drift", model="sonnet")


def test_a_malformed_project_file_is_refused_not_guessed_at(tmp_path, connection):
    _write_project_file(tmp_path, "torn", "{not json at all")

    [refusal] = _load_revision().backfill_project_records(connection, tmp_path)

    assert refusal.startswith("torn: project.json does not parse as JSON")
    assert _record_count(connection) == 0


def test_a_project_file_holding_a_list_is_refused(tmp_path, connection):
    _write_project_file(tmp_path, "listy", json.dumps([{"name": "listy"}]))

    [refusal] = _load_revision().backfill_project_records(connection, tmp_path)

    assert "holds a JSON list, not an object" in refusal
    assert _record_count(connection) == 0


def test_a_legacy_project_with_no_file_gets_no_invented_record(tmp_path, connection):
    (tmp_path / "legacy").mkdir()
    (tmp_path / "legacy" / "document.md").write_text("prose", encoding="utf-8")

    assert _load_revision().backfill_project_records(connection, tmp_path) == []

    assert _record_count(connection) == 0


def test_a_key_the_revision_cannot_place_is_refused_not_dropped(tmp_path, connection):
    _write_project_file(tmp_path, "extra", json.dumps(
        {"name": "extra", "model": "sonnet", "reviewer": "sam"}))

    [refusal] = _load_revision().backfill_project_records(connection, tmp_path)

    assert "unknown keys ['reviewer']" in refusal
    assert _record_count(connection) == 0


def test_a_file_naming_a_different_project_is_refused(tmp_path, connection):
    _write_project_file(tmp_path, "here", json.dumps({"name": "elsewhere"}))

    [refusal] = _load_revision().backfill_project_records(connection, tmp_path)

    assert "names the project 'elsewhere', but it sits in here/" in refusal
    assert _record_count(connection) == 0


def test_one_refused_project_does_not_stop_the_others_being_planned(tmp_path, connection):
    _write_project_file(tmp_path, "torn", "{not json at all")
    _write_project_file(tmp_path, "sound", json.dumps({"name": "sound", "model": "sonnet"}))

    refusals = _load_revision().backfill_project_records(connection, tmp_path)

    assert len(refusals) == 1
    # upgrade() raises on any refusal, which rolls this insert back with it.
    assert _stored(connection, "sound") is not None


def test_an_absent_projects_root_is_not_an_error(tmp_path, connection):
    assert _load_revision().backfill_project_records(connection, tmp_path / "nope") == []


def test_the_revision_declares_itself_irreversible(tmp_path, connection):
    with pytest.raises(NotImplementedError, match="not reversible"):
        _load_revision().downgrade()
