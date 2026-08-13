"""an eval dataset is a stored file, a file's project is an edge, a result is run-relative

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import uuid4

from alembic import op
from sqlalchemy.engine import Connection

from scripts.eval_dataset_files import (
    find_table_refs,
    locate_dataset_bytes,
    store_dataset_bytes,
    strip_run_prefix,
)

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# `uploaded_file` carried the project holding it. That pointer is now a `project_file`
# document, so a file can be claimed, refused or released without the record being
# rewritten — and so `TableRef` can address a file without addressing a project.
_FILES = "uploaded_file"
_EDGES = "project_file"
_EVALS = "eval"
_EVAL_RUNS = "eval_run"


def upgrade() -> None:
    connection = op.get_bind()
    _move_project_pointers_onto_edges(connection)
    _store_every_eval_dataset(connection)
    _make_every_result_ref_run_relative(connection)


def downgrade() -> None:
    raise NotImplementedError(
        "0014 is not reversible: a TableRef's path said where a file was on one machine "
        "and its sha256 says which bytes it is, so putting a path back would mean "
        "inventing one for a store that no longer has directories per project")


def _move_project_pointers_onto_edges(connection: Connection) -> None:
    for doc_id, data in _read_collection(connection, _FILES):
        document = json.loads(data)
        project_id = document.pop("project_id", None)
        _write_document(connection, _FILES, doc_id, document)
        if project_id:
            _write_document(connection, _EDGES, uuid4().hex,
                            _build_edge(str(project_id), doc_id))


# Driven by the DOCUMENTS this store holds, never by what is on disk. The two absences
# are different and must stay different: a store with no such eval — a fresh volume, a
# deploy whose projects were never these — has nothing to migrate and does no work,
# while a store that HAS the document and cannot find its bytes stops the upgrade.
# Only the file a document actually names is read, so a dataset someone versioned by
# hand leaves its earlier siblings in the directory untouched.
def _store_every_eval_dataset(connection: Connection) -> None:
    for doc_id, data in _read_collection(connection, _EVALS):
        document = json.loads(data)
        refs = find_table_refs(document)
        if not refs:
            continue
        project_id = str(document["project"])
        for ref in refs:
            ref["sha256"] = _adopt_dataset(connection, project_id, str(ref.pop("path")))
        _write_document(connection, _EVALS, doc_id, document)


def _make_every_result_ref_run_relative(connection: Connection) -> None:
    for doc_id, data in _read_collection(connection, _EVAL_RUNS):
        document = json.loads(data)
        result_ref = document.get("result_ref")
        # A vetoed run, and one that errored before scoring, wrote no result table.
        if not result_ref:
            continue
        document["result_ref"] = strip_run_prefix(str(result_ref), str(document["id"]))
        _write_document(connection, _EVAL_RUNS, doc_id, document)


def _adopt_dataset(connection: Connection, project_id: str, path: str) -> str:
    """Put an eval's dataset in the file store and return the sha256 its TableRef now holds."""
    digest, filename, byte_count = store_dataset_bytes(locate_dataset_bytes(path))
    # A project that already holds these bytes keeps the one record it has: the eval is
    # naming the same file its Files page lists, not a second claim on the same blob.
    if _find_file_of_project(connection, project_id, digest) is None:
        file_id = uuid4().hex
        _write_document(connection, _FILES, file_id,
                        _build_file(digest, filename, byte_count))
        _write_document(connection, _EDGES, uuid4().hex, _build_edge(project_id, file_id))
    return digest


def _find_file_of_project(
    connection: Connection, project_id: str, digest: str
) -> str | None:
    held = {json.loads(data)["file_id"] for _, data in _read_collection(connection, _EDGES)
            if json.loads(data)["project_id"] == project_id}
    return next((doc_id for doc_id, data in _read_collection(connection, _FILES)
                 if doc_id in held and json.loads(data)["sha256"] == digest), None)


def _build_file(digest: str, filename: str, byte_count: int) -> dict[str, Any]:
    return {**_stamp(), "sha256": digest, "filename": filename, "byte_count": byte_count}


def _build_edge(project_id: str, file_id: str) -> dict[str, Any]:
    return {**_stamp(), "project_id": project_id, "file_id": file_id}


def _stamp() -> dict[str, Any]:
    # The record is written now; nothing here knows when the bytes first arrived, and a
    # date read off the file would be the checkout's, not this workspace's.
    now = datetime.now().isoformat(timespec="microseconds")
    return {"created_at": now, "updated_at": now}


def _read_collection(connection: Connection, collection: str) -> list[tuple[str, str]]:
    return [(str(doc_id), data) for doc_id, data in connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (collection,)).fetchall()]


def _write_document(
    connection: Connection, collection: str, doc_id: str, document: dict[str, Any]
) -> None:
    connection.exec_driver_sql(
        "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
        "VALUES (?, ?, ?, ?)",
        (collection, doc_id, json.dumps({"id": doc_id, **document}), 1))
