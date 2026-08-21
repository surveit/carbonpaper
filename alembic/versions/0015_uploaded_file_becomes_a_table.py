"""uploaded_file moves from a documents blob to a table of columns

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

import json

from alembic import op

from app.core.files import UploadedFile
from app.core.persistence import find_table_spec

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# The first revision that changes the SHAPE of the store rather than the shape of a
# payload inside it. Every earlier one reads every row, edits JSON, and writes it back.
_COLLECTION = UploadedFile.collection


def upgrade() -> None:
    connection = op.get_bind()
    spec = find_table_spec(_COLLECTION)
    if spec is None:
        raise RuntimeError(f"{_COLLECTION} declares no table spec; STORED_AS_TABLE is off")
    connection.exec_driver_sql(spec.create_statement())
    rows = connection.exec_driver_sql(
        "SELECT id, data, schema_version FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    marks = ", ".join("?" * len(spec.columns))
    for doc_id, data, schema_version in rows:
        connection.exec_driver_sql(
            f"INSERT OR REPLACE INTO {spec.table} ({', '.join(spec.column_names())}) "
            f"VALUES ({marks})",
            tuple(spec.build_row(doc_id, json.loads(data), schema_version)),
        )
    connection.exec_driver_sql("DELETE FROM documents WHERE collection=?", (_COLLECTION,))


def downgrade() -> None:
    connection = op.get_bind()
    spec = find_table_spec(_COLLECTION)
    if spec is None:
        raise RuntimeError(f"{_COLLECTION} declares no table spec; STORED_AS_TABLE is off")
    rows = connection.exec_driver_sql(
        f"SELECT {', '.join(spec.column_names())} FROM {spec.table}"
    ).fetchall()
    for row in rows:
        body = spec.read_row(row)
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
            "VALUES (?, ?, ?, ?)",
            (_COLLECTION, body["id"], json.dumps(body), row[1]),
        )
    connection.exec_driver_sql(f"DROP TABLE {spec.table}")
