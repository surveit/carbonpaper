"""0021 renames the queue stage type, and moves what its old fingerprint keyed."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.core.persistence import configure_store
from app.core.sqlite_store import SqliteKvStore
from app.models.stage import parse_stage
from conftest import queue_added_columns, queue_columns, reads_of

_ALEMBIC_DIRECTORY = Path(__file__).resolve().parents[1] / "alembic"
_WAS = "human_review_queue"
_PROJECT = "proj"
_COLUMNS = [
    {"name": "id", "type": "str", "nullable": True},
    {"name": "score", "type": "int", "nullable": True},
]


def test_a_decision_recorded_under_the_old_name_is_still_found_after_the_rename(
    tmp_path, monkeypatch
):
    db_path = _open_file_backed_store(tmp_path, monkeypatch)
    stage = _queue_stage()
    was = parse_stage(stage).model_copy(update={"type": _WAS}).compute_definition_fingerprint()
    _write(db_path, "workflow_version", f"{_PROJECT}/v1",
           {"stages": [{**stage, "type": _WAS}]})
    _write(db_path, "stage_cache", f"v4/{_PROJECT}/gate/{was}/rowfp",
           {"project": _PROJECT, "stage_id": "gate", "stage_fingerprint": was,
            "input_fingerprint": "rowfp", "output_row": {"verdict": "approve"}})
    _write(db_path, "review_decision", "d1",
           {"project": _PROJECT, "stage_id": "gate", "stage_fingerprint": was,
            "input_fingerprint": "rowfp", "verdict": "approve"})

    command.upgrade(_alembic_config(), "head")

    now = parse_stage(_stored_stage(db_path)).compute_definition_fingerprint()
    assert now != was
    assert _stored_stage(db_path)["type"] == "review_queue"
    assert _ids(db_path, "stage_cache") == [f"v4/{_PROJECT}/gate/{now}/rowfp"]
    assert _load(db_path, "stage_cache", f"v4/{_PROJECT}/gate/{now}/rowfp")[
        "stage_fingerprint"] == now
    assert _load(db_path, "review_decision", "d1")["stage_fingerprint"] == now


def test_a_run_keeps_its_queue_counts_under_the_name_the_model_now_reads(
    tmp_path, monkeypatch
):
    """The field is required with no default, so a manifest left behind loads nowhere."""
    db_path = _open_file_backed_store(tmp_path, monkeypatch)
    _write(db_path, "run", f"{_PROJECT}/runs/r1", {
        "run_id": "r1", "project": _PROJECT,
        "human_review_queue_stats": {"gate": {"queued": 2, "decided": 1}},
        "stage_records": [{"stage_id": "gate", "type": _WAS}],
    })

    command.upgrade(_alembic_config(), "head")

    stored = _load(db_path, "run", f"{_PROJECT}/runs/r1")
    assert "human_review_queue_stats" not in stored
    assert stored["review_queue_stats"] == {"gate": {"queued": 2, "decided": 1}}
    assert stored["stage_records"][0]["type"] == "review_queue"


def _queue_stage() -> dict:
    return {
        "id": "gate", "description": "Review each row", "type": "review_queue",
        "inputs": [{"id": "load"}],
        "queue": {**queue_columns(), "reviewer_instructions": "Confirm each row."},
        "signature": {"form": "extends", "reads": reads_of("load", _COLUMNS),
                      "adds": queue_added_columns()},
    }


def _stored_stage(db_path: Path) -> dict:
    return _load(db_path, "workflow_version", f"{_PROJECT}/v1")["stages"][0]


def _open_file_backed_store(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(db_path))
    configure_store(SqliteKvStore(str(db_path)))
    return db_path


def _write(db_path: Path, collection: str, doc_id: str, data: dict) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO documents (collection, id, data, schema_version) VALUES (?,?,?,1)",
            (collection, doc_id, json.dumps({"id": doc_id, **data})),
        )


def _load(db_path: Path, collection: str, doc_id: str) -> dict:
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT data FROM documents WHERE collection=? AND id=?", (collection, doc_id)
        ).fetchone()
    assert row is not None, f"{collection}/{doc_id} is gone"
    return json.loads(row[0])


def _ids(db_path: Path, collection: str) -> list[str]:
    with sqlite3.connect(db_path) as connection:
        return [
            str(row[0]) for row in connection.execute(
                "SELECT id FROM documents WHERE collection=? ORDER BY id", (collection,)
            )
        ]


def _alembic_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIRECTORY))
    return config
