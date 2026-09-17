"""0022 answers `no_kind` on the stages a mechanical rule settles, and on no others."""
from __future__ import annotations

import copy
import importlib.util
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_ALEMBIC_DIRECTORY = _REPO_ROOT / "alembic"
_REVISION = _ALEMBIC_DIRECTORY / "versions/0022_stages_whose_rows_are_not_a_kind_of_thing_say_so.py"
_STAGE_SPEC_SCHEMA_VERSION = 8


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0022", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _a_version_of_every_answering_type() -> dict[str, Any]:
    return {"stages": [
        {"id": "load", "type": "input_data"},
        {"id": "per_facility", "type": "aggregate",
         "aggregate": {"group_by": ["facility_id"], "aggregations": []}},
        {"id": "one_figure", "type": "aggregate",
         "aggregate": {"group_by": [], "aggregations": []}},
        {"id": "fined_only", "type": "filter_rows"},
        {"id": "write_the_cards", "type": "report", "report": {"format": "evidence_cards"}},
    ]}


def _row_type_ids(document: dict[str, Any]) -> dict[str, Any]:
    return {stage["id"]: stage.get("row_type_id") for stage in document["stages"]}


def test_the_report_and_the_ungrouped_aggregate_are_answered_and_nothing_else_is():
    revision = _load_revision()
    document = _a_version_of_every_answering_type()

    assert revision.answer_no_kind_where_the_type_settles_it(document) is True

    assert _row_type_ids(document) == {
        "load": None, "per_facility": None, "one_figure": "no_kind",
        "fined_only": None, "write_the_cards": "no_kind",
    }


def test_no_word_is_guessed_for_a_source_or_a_grouped_aggregate():
    # The two the rule cannot settle: guessing one would be the fabrication a warning exists for.
    revision = _load_revision()
    document = _a_version_of_every_answering_type()

    revision.answer_no_kind_where_the_type_settles_it(document)

    assert "row_type_id" not in document["stages"][0]
    assert "row_type_id" not in document["stages"][1]


def test_a_word_an_author_wrote_is_left_alone():
    revision = _load_revision()
    document = {"stages": [
        {"id": "per_facility", "type": "aggregate", "row_type_id": "facility",
         "aggregate": {"group_by": ["facility_id"], "aggregations": []}},
    ]}

    assert revision.answer_no_kind_where_the_type_settles_it(document) is False
    assert document["stages"][0]["row_type_id"] == "facility"


def test_a_replay_over_an_answered_store_changes_nothing():
    revision = _load_revision()
    document = _a_version_of_every_answering_type()
    revision.answer_no_kind_where_the_type_settles_it(document)
    answered = copy.deepcopy(document)

    assert revision.answer_no_kind_where_the_type_settles_it(document) is False
    assert document == answered


def test_a_document_holding_no_stages_at_all_is_left_alone():
    revision = _load_revision()
    assert revision.answer_no_kind_where_the_type_settles_it({"name": "terms"}) is False


def test_an_aggregate_storing_no_group_by_stops_the_backfill():
    # Nothing here can tell one row per group from one figure, and neither answer may be guessed.
    revision = _load_revision()
    document = {"stages": [{"id": "totals", "type": "aggregate"}]}

    with pytest.raises(ValueError, match="stores no `group_by`"):
        revision.answer_no_kind_where_the_type_settles_it(document)


def test_the_downgrade_takes_back_every_answer_the_upgrade_wrote():
    revision = _load_revision()
    document = _a_version_of_every_answering_type()
    before = copy.deepcopy(document)

    revision.answer_no_kind_where_the_type_settles_it(document)
    assert revision.strip_the_backfilled_answer(document) is True

    assert document == before


def test_the_downgrade_leaves_a_word_an_author_wrote_where_it_is():
    revision = _load_revision()
    document = {"stages": [
        {"id": "per_facility", "type": "aggregate", "row_type_id": "facility",
         "aggregate": {"group_by": ["facility_id"], "aggregations": []}},
    ]}

    assert revision.strip_the_backfilled_answer(document) is False
    assert document["stages"][0]["row_type_id"] == "facility"


def test_a_downgrade_over_an_unanswered_store_changes_nothing():
    revision = _load_revision()
    document = _a_version_of_every_answering_type()

    assert revision.strip_the_backfilled_answer(document) is False
    assert document == _a_version_of_every_answering_type()


# ── through the store, so the collections and the SQL are the ones that run ──
def _migrate_store_to(revision: str) -> None:
    config = Config()
    config.set_main_option("script_location", str(_ALEMBIC_DIRECTORY))
    command.upgrade(config, revision)


def _store_holding_a_version_in_every_collection(tmp_path, monkeypatch) -> Path:
    db_path = tmp_path / "app.db"
    monkeypatch.setenv("CARBON_PAPER_DB_PATH", str(db_path))
    _migrate_store_to("0021")
    connection = sqlite3.connect(db_path)
    try:
        for collection in ("workflow_version", "working_copy", "draft"):
            connection.execute(
                "INSERT INTO documents (collection, id, data, schema_version) VALUES (?,?,?,?)",
                (collection, f"proj/{collection}",
                 json.dumps(_a_version_of_every_answering_type()),
                 _STAGE_SPEC_SCHEMA_VERSION),
            )
        connection.commit()
    finally:
        connection.close()
    return db_path


def _read_stored_documents(db_path: Path) -> dict[str, tuple[dict[str, Any], int]]:
    connection = sqlite3.connect(db_path)
    try:
        rows = connection.execute(
            "SELECT collection, data, schema_version FROM documents").fetchall()
    finally:
        connection.close()
    return {str(c): (json.loads(data), int(version)) for c, data, version in rows}


def test_every_collection_embedding_stage_specs_is_answered(tmp_path, monkeypatch):
    db_path = _store_holding_a_version_in_every_collection(tmp_path, monkeypatch)

    _migrate_store_to("0022")

    stored = _read_stored_documents(db_path)
    assert sorted(stored) == ["draft", "workflow_version", "working_copy"]
    for document, _version in stored.values():
        assert _row_type_ids(document)["write_the_cards"] == "no_kind"
        assert _row_type_ids(document)["one_figure"] == "no_kind"


def test_the_backfill_leaves_the_stage_spec_schema_version_where_it_was(tmp_path, monkeypatch):
    # The counter tracks shapes a reader must be migrated to; an optional field is not one.
    db_path = _store_holding_a_version_in_every_collection(tmp_path, monkeypatch)

    _migrate_store_to("0022")

    versions = {version for _document, version in _read_stored_documents(db_path).values()}
    assert versions == {_STAGE_SPEC_SCHEMA_VERSION}
