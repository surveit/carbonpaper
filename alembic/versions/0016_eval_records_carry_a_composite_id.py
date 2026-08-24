"""an eval config/run becomes a record: local id to `eval_id`/`run_id`, `id` composite

Revision ID: 0016
Revises: 0015
"""
from __future__ import annotations

import json

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# collection -> the field its local slug moves to. The document key is already
# `{project}/{slug}`, so `id` takes that key and the slug keeps its own name.
_LOCAL_ID_FIELD = {"eval": "eval_id", "eval_run": "run_id"}


def upgrade() -> None:
    for collection, local_field in _LOCAL_ID_FIELD.items():
        _move_local_id(collection, "id", local_field, compose=True)


def downgrade() -> None:
    for collection, local_field in _LOCAL_ID_FIELD.items():
        _move_local_id(collection, local_field, "id", compose=False)


def _move_local_id(collection: str, was: str, now: str, *, compose: bool) -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (collection,)
    ).fetchall()
    for record_id, data in rows:
        payload = json.loads(data)
        if was not in payload or now in payload:
            continue
        payload[now] = payload.pop(was)
        if compose:
            payload["id"] = record_id
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection=? AND id=?",
            (json.dumps(payload), collection, record_id),
        )
