"""A v1 document, run through 0002, must satisfy today's model.

This is the load CI never did: every other test builds its input from the current
models, so a widened required field passes everything and still orphans the store.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.models.workflow import parse_workflow

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0002_name_queue_and_join_columns.py")


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0002", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _column(name: str) -> dict[str, Any]:
    return {"name": name, "type": "str", "nullable": True}


def _v1_stages() -> list[dict[str, Any]]:
    """A v1 workflow: a queue naming no columns, and an enrich naming no brought ones."""
    subject = {"columns": [_column("id"), _column("verdict")]}
    reference = {"columns": [_column("id"), _column("extra")]}
    return [
        {"id": "src", "name": "Source", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv", "path": "/tmp/a.csv"}},
         "inputs": [], "output_schema": subject},
        {"id": "ref", "name": "Reference", "type": "input_data",
         "connector": {"kind": "file", "params": {"format": "csv", "path": "/tmp/b.csv"}},
         "inputs": [], "output_schema": reference},
        {"id": "joined", "name": "Join", "type": "enrich",
         "inputs": [{"id": "src", "schema": subject}, {"id": "ref", "schema": reference}],
         "join": {"keys": [{"left": "id", "right": "id"}]},
         "output_schema": {"columns": [_column("id"), _column("verdict"), _column("extra")]}},
        {"id": "gate", "name": "Review", "type": "human_review_queue",
         "inputs": [{"id": "joined", "schema": {
             "columns": [_column("id"), _column("verdict"), _column("extra")]}}],
         "queue": {"reviewer_instructions": "Confirm each row."},
         "output_schema": {"columns": [
             _column("id"), _column("verdict"), _column("extra"),
             _column("decision"), _column("reviewer_id"), _column("reviewed_at")]}},
    ]


def test_a_v1_document_validates_under_todays_model_after_upgrading():
    rev = _load_revision()
    rev._REVIEWED_COLUMNS_BY_PROJECT["proj"] = {"verdict": "verdict_reviewed"}
    document = {"stages": _v1_stages()}
    rev._upgrade_document(document, "proj")

    parse_workflow(document["stages"])  # raises if the upgraded shape is still invalid

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
