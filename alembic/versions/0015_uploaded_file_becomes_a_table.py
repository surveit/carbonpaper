"""uploaded_file becomes a table of columns

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_COLLECTION = "uploaded_file"

# A FROZEN copy of what `alembic revision --autogenerate` read off UploadedFile, not a
# reference to the live model: a field added later must not change what this revision did.
# `checkfirst`, and the delete that follows, are what make replaying it at head a no-op —
# the store creates this table itself when it opens a fresh database, exactly as 0001 does
# for `documents`.
_TABLE = sa.Table(
    _COLLECTION, sa.MetaData(),
    sa.Column("id", sa.Text(), primary_key=True),
    sa.Column("schema_version", sa.Integer(), nullable=False),
    sa.Column("created_at", sa.Text(), nullable=False),
    sa.Column("updated_at", sa.Text(), nullable=False),
    sa.Column("sha256", sa.Text(), nullable=False),
    sa.Column("filename", sa.Text(), nullable=False),
    sa.Column("byte_count", sa.Integer(), nullable=False),
    sa.Column("project_id", sa.Text(), nullable=True),
)


def upgrade() -> None:
    connection = op.get_bind()
    _TABLE.create(connection, checkfirst=True)
    rows = connection.exec_driver_sql(
        "SELECT data, schema_version FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for data, schema_version in rows:
        connection.execute(
            sa.insert(_TABLE).prefix_with("OR REPLACE").values(
                schema_version=schema_version, **json.loads(data)))
    connection.exec_driver_sql("DELETE FROM documents WHERE collection=?", (_COLLECTION,))


def downgrade() -> None:
    connection = op.get_bind()
    for row in connection.execute(sa.select(_TABLE)):
        body = {name: value for name, value in row._mapping.items() if name != "schema_version"}
        connection.exec_driver_sql(
            "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
            "VALUES (?, ?, ?, ?)",
            (_COLLECTION, body["id"], json.dumps(body), row.schema_version))
    op.drop_table(_COLLECTION)
