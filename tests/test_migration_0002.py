"""A v1 document, run through 0002, must satisfy today's model.

This is the load CI never did: every other test builds its input from the current
models, so a widened required field passes everything and still orphans the store.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

from app.models.workflow import parse_workflow
from conftest import drop_input_schemas
from scripts.stage_signatures import add_signature

_ALEMBIC_DIRECTORY = Path(__file__).resolve().parents[1] / "alembic"
_REVISION = _ALEMBIC_DIRECTORY / "versions/0002_name_queue_and_join_columns.py"


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0002", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column(name: str) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True}


def _v1_stages() -> list[dict[str, Any]]:
    subject = {"columns": [_column("id"), _column("verdict")]}
    reference = {"columns": [_column("id"), _column("extra")]}
    return [
        {"id": "src", "description": "Source", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv", "path": "/tmp/a.csv"}},
         "inputs": [], "output_schema": subject},
        {"id": "ref", "description": "Reference", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv", "path": "/tmp/b.csv"}},
         "inputs": [], "output_schema": reference},
        {"id": "joined", "description": "Join", "type": "enrich",
         "inputs": [{"id": "src", "schema": subject}, {"id": "ref", "schema": reference}],
         "join": {"keys": [{"left": "id", "right": "id"}]},
         "output_schema": {"columns": [_column("id"), _column("verdict"), _column("extra")]}},
        {"id": "gate", "description": "Review", "type": "human_review_queue",
         "inputs": [{"id": "joined", "schema": {
             "columns": [_column("id"), _column("verdict"), _column("extra")]}}],
         "queue": {"reviewer_instructions": "Confirm each row."},
         "output_schema": {"columns": [
             _column("id"), _column("verdict"), _column("extra"),
             _column("decision"), _column("reviewer_id"), _column("reviewed_at"),
         ]}},
    ]


def _v2_stages() -> list[dict[str, Any]]:
    """The shape current code writes: named columns, and no stored schema to read them off."""
    return [
        {"id": "src", "description": "Source", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv"}}, "inputs": []},
        {"id": "ref", "description": "Reference", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv"}}, "inputs": []},
        {"id": "joined", "description": "Join", "type": "enrich",
         "inputs": [{"id": "src"}, {"id": "ref"}],
         "join": {"keys": [{"left": "id", "right": "id"}],
                  "enrich_with": {"extra": "extra"}}},
        {"id": "gate", "description": "Review", "type": "human_review_queue",
         "inputs": [{"id": "joined"}],
         "queue": {"reviewer_instructions": "Confirm each row.",
                   "reviewed_columns": {"verdict": "reviewed_verdict"},
                   "verdict_column": "review_verdict", "reviewer_column": "reviewer",
                   "reviewed_at_column": "reviewed_at"}},
    ]


def _upgrade_store_to(revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIRECTORY))
    command.upgrade(config, revision)


def _store_holding(
    tmp_path, monkeypatch, stages: list[dict[str, Any]], schema_version: int
) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(db_path))
    _upgrade_store_to("0001")
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "INSERT INTO documents (collection, id, data, schema_version) VALUES (?,?,?,?)",
            ("workflow_version", "proj/v1", json.dumps({"stages": stages}), schema_version),
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def _stored_version(db_path: Path) -> tuple[str, int]:
    connection = sqlite3.connect(db_path)
    try:
        row = connection.execute(
            "SELECT data, schema_version FROM documents WHERE id='proj/v1'").fetchone()
    finally:
        connection.close()
    return str(row[0]), int(row[1])


def test_a_record_already_naming_its_columns_is_not_a_decision_anyone_owes(
        tmp_path, monkeypatch):
    """The live store's one version: written by current code, so 0002 has nothing to do."""
    db_path = _store_holding(tmp_path, monkeypatch, _v2_stages(), schema_version=4)
    before = _stored_version(db_path)

    _upgrade_store_to("0002")

    # Untouched, schema_version included — 0002's own stamp of 2 would walk it backwards.
    assert _stored_version(db_path) == before


def test_a_v1_queue_with_no_decision_recorded_is_still_refused(tmp_path, monkeypatch):
    db_path = _store_holding(tmp_path, monkeypatch, _v1_stages(), schema_version=1)

    # alembic loads its own copy of the file, so the NAME identifies the class.
    with pytest.raises(Exception, match=r"human decision.*\['proj'\]") as refusal:
        _upgrade_store_to("0002")

    assert type(refusal.value).__name__ == "UnmigratableRecord"
    # Refused, not half-done: the record is still v1, waiting for the entry to be added.
    assert _stored_version(db_path)[1] == 1


def test_a_v1_document_validates_under_todays_model_after_upgrading():
    rev = _load_revision()
    rev._REVIEWED_COLUMNS_BY_PROJECT["proj"] = {"verdict": "verdict_reviewed"}
    document = {"stages": _v1_stages()}
    rev._name_stage_columns(rev._find_unnamed_stages(document), "proj")

    # 0002 brings the document to ITS shape; 0006's synthesis carries it the rest
    # of the way, as a store crossing both revisions would be.
    for stage in document["stages"]:
        add_signature(stage)
    parse_workflow([drop_input_schemas(s) for s in document["stages"]])

    queue = document["stages"][3]["queue"]
    assert queue["verdict_column"] == "decision"
    assert queue["reviewed_columns"] == {"verdict": "verdict_reviewed"}
    assert document["stages"][2]["join"]["enrich_with"] == {"extra": "extra"}


def test_an_output_column_from_neither_input_is_refused_not_guessed():
    rev = _load_revision()
    stage = _v1_stages()[2]
    stage["output_schema"]["columns"].append(_column("from_nowhere"))
    with pytest.raises(rev.UnmigratableRecord, match="from_nowhere"):
        rev._name_brought_columns(stage)


def test_a_project_with_no_reviewed_column_decision_is_refused():
    rev = _load_revision()
    rev._REVIEWED_COLUMNS_BY_PROJECT.pop("undecided", None)
    with pytest.raises(KeyError):
        rev._name_queue_columns(_v1_stages()[3], "undecided")
