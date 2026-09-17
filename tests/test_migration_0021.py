"""0021 splits every stored noun into the row type its rows are and the table holding them."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from app.models.records.terms import StoredTerms

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REVISION = _REPO_ROOT / "alembic/versions/0021_a_noun_splits_into_a_row_type_and_a_table.py"
_PALM_OIL = _REPO_ROOT / "tests/fixtures/palm_oil_mill_register_terms_v1.json"


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0021", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _palm_oil_terms() -> dict[str, Any]:
    """The mill register's own stored document, copied off a live store at v1."""
    return json.loads(_PALM_OIL.read_text(encoding="utf-8"))


def _nouns(document: dict[str, Any]) -> list[dict[str, Any]]:
    return document["nouns"]["schemas"]


def test_the_fixture_is_the_shape_the_migration_was_written_for():
    document = _palm_oil_terms()
    assert sorted(document) == ["created_at", "id", "nouns", "updated_at", "verbs"]
    assert len(_nouns(document)) == 13
    assert sum(1 for noun in _nouns(document) if noun["columns"]) == 5
    assert len(document["verbs"]) == 9


def test_every_noun_mints_a_row_type_and_only_the_tables_stay_tables():
    revision = _load_revision()
    document = _palm_oil_terms()

    assert revision.split_stored_nouns(document) is True

    assert "nouns" not in document
    assert len(document["row_types"]) == 13
    assert [table["name"] for table in document["schemas"]["schemas"]] == [
        "record", "source", "document", "passage", "claim"]


def test_the_word_with_no_table_keeps_its_definition_and_the_table_keeps_its_columns():
    # mill carries the register's definition and no columns; record carries the 22 columns.
    revision = _load_revision()
    document = _palm_oil_terms()

    revision.split_stored_nouns(document)

    by_id = {row_type["id"]: row_type for row_type in document["row_types"]}
    assert by_id["mill"]["definition"].startswith("One physical palm oil mill")
    assert "mill" not in {t["name"] for t in document["schemas"]["schemas"]}
    record = next(t for t in document["schemas"]["schemas"] if t["name"] == "record")
    assert len(record["columns"]) == 22


def test_the_split_keeps_no_second_spelling_of_any_word():
    # Every spelling the register held was the prose form of its own id, which `title` says.
    revision = _load_revision()
    document = _palm_oil_terms()

    revision.split_stored_nouns(document)

    assert all("also_written" not in t for t in document["schemas"]["schemas"])
    assert all("also_written" not in r for r in document["row_types"])
    assert all("also_written" not in v for v in document["verbs"])


def test_the_verbs_keep_every_value_but_their_spellings():
    revision = _load_revision()
    document = _palm_oil_terms()

    revision.split_stored_nouns(document)

    assert document["verbs"] == [
        {key: value for key, value in verb.items() if key != "also_written"}
        for verb in _palm_oil_terms()["verbs"]
    ]


def test_the_split_document_is_what_the_record_now_loads():
    revision = _load_revision()
    document = _palm_oil_terms()

    revision.split_stored_nouns(document)

    stored = StoredTerms.model_validate(document)
    assert [row_type.id for row_type in stored.row_types][:2] == ["mill", "record"]
    assert [schema.name for schema in stored.schemas.schemas] == [
        "record", "source", "document", "passage", "claim"]


def test_the_unsplit_document_is_refused_by_the_record_it_is_stored_as():
    # Why the migration exists rather than a tolerant reader.
    with pytest.raises(Exception, match="nouns"):
        StoredTerms.model_validate(_palm_oil_terms())


def test_a_replay_over_a_split_store_changes_nothing():
    revision = _load_revision()
    document = _palm_oil_terms()
    revision.split_stored_nouns(document)
    split = copy.deepcopy(document)

    assert revision.split_stored_nouns(document) is False
    assert document == split


def test_the_downgrade_returns_every_value_the_mill_register_held():
    revision = _load_revision()
    document = _palm_oil_terms()

    revision.split_stored_nouns(document)
    assert revision.fuse_row_types_back_into_nouns(document) is True

    for fused, held in zip(_nouns(document), _nouns(_palm_oil_terms()), strict=True):
        # Spellings do not come back — v2 stores none — and neither do the keys the
        # register held nothing in.
        held = {key: value for key, value in held.items() if key != "also_written"}
        assert fused == {key: value for key, value in held.items() if value or key in fused}


def test_splitting_a_downgraded_store_again_lands_where_the_first_split_did():
    revision = _load_revision()
    document = _palm_oil_terms()
    revision.split_stored_nouns(document)
    split = copy.deepcopy(document)

    revision.fuse_row_types_back_into_nouns(document)
    revision.split_stored_nouns(document)

    assert document["row_types"] == split["row_types"]


def test_a_downgrade_over_an_unsplit_store_changes_nothing():
    revision = _load_revision()
    document = _palm_oil_terms()

    assert revision.fuse_row_types_back_into_nouns(document) is False
    assert document == _palm_oil_terms()


def test_a_noun_carrying_no_name_stops_the_split():
    revision = _load_revision()
    document = {"nouns": {"schemas": [{"title": "Mill", "columns": []}]}, "verbs": []}

    with pytest.raises(ValueError, match="row type"):
        revision.split_stored_nouns(document)
