"""0014 splits a file's project onto an edge, stores every eval dataset, and makes a
result_ref relative to the run that wrote it."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from scripts.eval_dataset_files import (
    EvalDatasetMissing,
    EvalResultRefUnreadable,
    find_table_refs,
    locate_dataset_bytes,
    store_dataset_bytes,
    strip_run_prefix,
)

_REVISION = (Path(__file__).resolve().parents[1]
             / "alembic/versions/0014_eval_datasets_are_stored_files.py")

_CSV = b"k,v\n1,2\n"
_CSV_SHA = hashlib.sha256(_CSV).hexdigest()


def _load_revision() -> Any:
    spec = importlib.util.spec_from_file_location("_rev_0014", _REVISION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── which refs a config carries ──────────────────────────────────────────────

def test_both_an_evals_own_table_and_its_reference_overrides_are_found():
    document = {
        "table": {"path": "a.csv"},
        "reference_overrides": [{"stage_id": "s", "table": {"path": "b.csv"}}],
    }

    assert [ref["path"] for ref in find_table_refs(document)] == ["a.csv", "b.csv"]


def test_a_ref_already_carrying_a_sha256_is_left_alone():
    assert find_table_refs({"table": {"sha256": _CSV_SHA}}) == []


def test_an_eval_with_no_table_yet_carries_nothing_to_convert():
    assert find_table_refs({"reference_overrides": []}) == []


# ── locating the bytes a path names ──────────────────────────────────────────

def test_a_path_under_the_checkout_is_found(tmp_path, monkeypatch):
    from app.core import paths
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "cases.csv").write_bytes(_CSV)

    assert locate_dataset_bytes("seeds/cases.csv").read_bytes() == _CSV


def test_a_path_under_the_storage_home_is_found_too(tmp_path, monkeypatch):
    import scripts.eval_dataset_files as module
    # The store moved out of the checkout carrying `examples/` with it, so a recorded
    # path may resolve here and nowhere else.
    monkeypatch.setattr(module, "CARBON_PAPER_HOME", tmp_path)
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "cases.csv").write_bytes(_CSV)

    assert locate_dataset_bytes("examples/cases.csv").read_bytes() == _CSV


def test_a_path_on_neither_root_stops_the_migration(tmp_path, monkeypatch):
    from app.core import paths
    import scripts.eval_dataset_files as module
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(module, "CARBON_PAPER_HOME", tmp_path)

    with pytest.raises(EvalDatasetMissing, match="gone.csv"):
        locate_dataset_bytes("gone.csv")


# ── the content-addressed copy ───────────────────────────────────────────────

def test_a_dataset_lands_where_an_upload_would(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    source = tmp_path / "cases.csv"
    source.write_bytes(_CSV)

    digest, filename, byte_count = store_dataset_bytes(source)

    assert (digest, filename, byte_count) == (_CSV_SHA, "cases.csv", len(_CSV))
    assert (tmp_path / "files" / _CSV_SHA / "cases.csv").read_bytes() == _CSV


def test_bytes_the_store_already_holds_are_not_written_a_second_time(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    (tmp_path / "files" / _CSV_SHA).mkdir(parents=True)
    (tmp_path / "files" / _CSV_SHA / "first_name.csv").write_bytes(_CSV)
    source = tmp_path / "second_name.csv"
    source.write_bytes(_CSV)

    assert store_dataset_bytes(source)[0] == _CSV_SHA
    # The name the bytes were FIRST stored as is what resolve_stored_path builds from.
    assert [p.name for p in (tmp_path / "files" / _CSV_SHA).iterdir()] == ["first_name.csv"]


# ── result_ref becomes run-relative ──────────────────────────────────────────

def test_a_result_ref_loses_the_prefix_the_runner_put_in_front_of_it():
    assert strip_run_prefix("eval_run/r1/result.parquet", "r1") == "result.parquet"


def test_a_result_ref_naming_another_run_is_refused_rather_than_stripped():
    with pytest.raises(EvalResultRefUnreadable, match="eval_run/r1/"):
        strip_run_prefix("eval_run/r2/result.parquet", "r1")


def test_an_already_run_relative_ref_is_refused_rather_than_stripped_twice():
    with pytest.raises(EvalResultRefUnreadable):
        strip_run_prefix("result.parquet", "r1")


# ── the whole upgrade, over a real sqlite store ──────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real SQLAlchemy connection over a real file — what alembic/env.py hands a revision."""
    from sqlalchemy import create_engine
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    from app.core import paths
    monkeypatch.setattr(paths, "REPO_ROOT", tmp_path)
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'app.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE documents (collection TEXT NOT NULL, id TEXT NOT NULL, "
            "data TEXT NOT NULL, schema_version INTEGER NOT NULL DEFAULT 1, "
            "PRIMARY KEY (collection, id))")
        yield connection


def _write(connection, collection: str, doc_id: str, document: dict) -> None:
    connection.exec_driver_sql(
        "INSERT INTO documents (collection, id, data) VALUES (?, ?, ?)",
        (collection, doc_id, json.dumps(document)))


def _read_all(connection, collection: str) -> list[dict]:
    return [json.loads(data) for (data,) in connection.exec_driver_sql(
        "SELECT data FROM documents WHERE collection=?", (collection,)).fetchall()]


def test_a_files_project_moves_onto_an_edge(store):
    _write(store, "uploaded_file", "f1",
           {"id": "f1", "sha256": _CSV_SHA, "filename": "a.csv", "byte_count": 8,
            "project_id": "demo", "created_at": "t", "updated_at": "t"})

    _load_revision()._move_project_pointers_onto_edges(store)

    assert "project_id" not in _read_all(store, "uploaded_file")[0]
    (edge,) = _read_all(store, "project_file")
    assert (edge["project_id"], edge["file_id"]) == ("demo", "f1")


def test_a_file_no_project_held_gets_no_edge(store):
    _write(store, "uploaded_file", "f1",
           {"id": "f1", "sha256": _CSV_SHA, "filename": "a.csv", "byte_count": 8,
            "project_id": None, "created_at": "t", "updated_at": "t"})

    _load_revision()._move_project_pointers_onto_edges(store)

    assert _read_all(store, "project_file") == []


def test_an_evals_path_becomes_the_sha256_of_a_file_its_project_now_holds(store, tmp_path):
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "cases.csv").write_bytes(_CSV)
    _write(store, "eval", "demo/e1",
           {"id": "e1", "project": "demo", "table": {"path": "seeds/cases.csv"}})

    _load_revision()._store_every_eval_dataset(store)

    assert _read_all(store, "eval")[0]["table"] == {"sha256": _CSV_SHA}
    (record,) = _read_all(store, "uploaded_file")
    assert record["sha256"] == _CSV_SHA
    (edge,) = _read_all(store, "project_file")
    assert (edge["project_id"], edge["file_id"]) == ("demo", record["id"])


def test_two_evals_of_one_project_on_the_same_bytes_share_its_one_file(store, tmp_path):
    (tmp_path / "seeds").mkdir()
    (tmp_path / "seeds" / "cases.csv").write_bytes(_CSV)
    for eval_id in ("e1", "e2"):
        _write(store, "eval", f"demo/{eval_id}",
               {"id": eval_id, "project": "demo", "table": {"path": "seeds/cases.csv"}})

    _load_revision()._store_every_eval_dataset(store)

    assert len(_read_all(store, "uploaded_file")) == 1
    assert len(_read_all(store, "project_file")) == 1


def test_a_store_holding_no_evals_has_nothing_to_migrate(store):
    """A fresh volume is not a failure: absence of the DOCUMENT is checked, not of the bytes."""
    _load_revision()._store_every_eval_dataset(store)

    assert _read_all(store, "uploaded_file") == []


def test_an_eval_whose_bytes_are_gone_stops_the_upgrade_rather_than_guessing(store):
    _write(store, "eval", "demo/e1",
           {"id": "e1", "project": "demo", "table": {"path": "seeds/gone.csv"}})

    with pytest.raises(EvalDatasetMissing, match="gone.csv"):
        _load_revision()._store_every_eval_dataset(store)

    # No placeholder sha256, and the path is still there to try again with.
    assert _read_all(store, "eval")[0]["table"] == {"path": "seeds/gone.csv"}
    assert _read_all(store, "uploaded_file") == []


def test_the_real_stored_eval_is_migrated_and_its_hand_made_sibling_is_left_alone(
    store, tmp_path, monkeypatch
):
    """The one eval in the user's store: a project-owned parquet, versioned by hand to _v2."""
    import scripts.eval_dataset_files as module
    monkeypatch.setattr(module, "CARBON_PAPER_HOME", tmp_path)
    data = tmp_path / "examples" / "hate_on_activist_pages" / "eval_data"
    data.mkdir(parents=True)
    (data / "climate_scope_and_direction.parquet").write_bytes(b"the v1 nobody referenced")
    (data / "climate_scope_and_direction_v2.parquet").write_bytes(_CSV)
    _write(store, "eval", "hate_on_activist_pages/climate-scope-and-direction",
           {"id": "climate-scope-and-direction", "project": "hate_on_activist_pages",
            "table": {"path": "examples/hate_on_activist_pages/eval_data/"
                              "climate_scope_and_direction_v2.parquet",
                      "format": "parquet"}})

    _load_revision()._store_every_eval_dataset(store)

    assert _read_all(store, "eval")[0]["table"] == {"sha256": _CSV_SHA, "format": "parquet"}
    # Exactly one file: the migration reads what the document names and sweeps nothing.
    assert [r["filename"] for r in _read_all(store, "uploaded_file")] == [
        "climate_scope_and_direction_v2.parquet"]


def test_a_stored_result_ref_becomes_run_relative(store):
    _write(store, "eval_run", "demo/r1",
           {"id": "r1", "result_ref": "eval_run/r1/result.parquet"})

    _load_revision()._make_every_result_ref_run_relative(store)

    assert _read_all(store, "eval_run")[0]["result_ref"] == "result.parquet"


def test_a_run_that_scored_nothing_keeps_its_absent_result_ref(store):
    _write(store, "eval_run", "demo/vetoed", {"id": "vetoed", "result_ref": None})

    _load_revision()._make_every_result_ref_run_relative(store)

    assert _read_all(store, "eval_run")[0]["result_ref"] is None
