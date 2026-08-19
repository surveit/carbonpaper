"""a run-event chunk records where it starts, instead of it being read off the index

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import json

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# Every chunk written before this field was CHUNK_SIZE 500, and the writer kept
# them dense with only the last one partial, so chunk k starts at exactly k*500.
# Verified against 393 stored chunks across 19 runs, resumed ones included: zero
# disagreements, and no non-final chunk holding anything but 500.
_SIZE_WHEN_WRITTEN = 500
_COLLECTION = "run_events"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("first_seq") is not None:
            continue                      # re-runnable: a chunk already carrying one is left alone
        document["first_seq"] = _chunk_index(str(doc_id)) * _SIZE_WHEN_WRITTEN
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection=? AND id=?",
            (json.dumps(document), _COLLECTION, str(doc_id)),
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if isinstance(document, dict) and document.pop("first_seq", None) is not None:
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), _COLLECTION, str(doc_id)),
            )


def _chunk_index(doc_id: str) -> int:
    """`<project>/<run>/<index:06d>` — the index is the last segment."""
    return int(doc_id.rsplit("/", 1)[1])
