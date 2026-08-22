"""a project file's `status` becomes `completeness`

Revision ID: 0015
Revises: 0014
"""
from __future__ import annotations

import json

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_COLLECTION = "uploaded_file"
_WAS = "status"
_NOW = "completeness"


def upgrade() -> None:
    _rename_field(_WAS, _NOW)


def downgrade() -> None:
    _rename_field(_NOW, _WAS)


def _rename_field(was: str, now: str) -> None:
    # The model forbids an unknown field, so a record still carrying the old name fails to
    # load at all rather than reading as unset.
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for record_id, data in rows:
        payload = json.loads(data)
        if was not in payload:
            continue
        payload[now] = payload.pop(was)
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection=? AND id=?",
            (json.dumps(payload), _COLLECTION, record_id),
        )
