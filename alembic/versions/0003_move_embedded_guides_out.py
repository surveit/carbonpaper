"""move an embedded review guide into its own record

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# v1 embedded the guide in the version document; it is its own record now, and the
# version model forbids the leftover key. The prose is authored by a human and
# exists nowhere else, so it is COPIED OUT before the key is dropped, never
# discarded. Literals, not imports: a migration must keep reading v1 forever.
_GUIDE_KEY = "guide"
_GUIDE_COLLECTION = "review_guide"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        guide = document.pop(_GUIDE_KEY, None)
        if guide is None:
            continue
        _write_guide(connection, str(doc_id), guide)
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection='workflow_version' AND id=?",
            (json.dumps(document), str(doc_id)),
        )


def downgrade() -> None:
    raise NotImplementedError("0003 is not reversible: the guide record is now the only copy")


def _write_guide(connection: Any, doc_id: str, guide: dict[str, Any]) -> None:
    """One review_guide record, its backpointers read off the version's own id."""
    project, _, version_id = doc_id.partition("/")
    stamp = _read_stamp(connection, doc_id)
    record = {
        "id": str(uuid.uuid4()),
        "project": project,
        "version_id": version_id,
        "steps": guide.get("steps", []),
        "unnarrated": guide.get("unnarrated", []),
        "created_at": stamp,
        "updated_at": stamp,
    }
    connection.exec_driver_sql(
        "INSERT OR REPLACE INTO documents (collection, id, data, schema_version) "
        "VALUES (?, ?, ?, 1)",
        (_GUIDE_COLLECTION, record["id"], json.dumps(record)),
    )


def _read_stamp(connection: Any, doc_id: str) -> str:
    """The version's own updated_at — the guide was written with it, so inventing a
    fresh timestamp here would date the prose to the migration instead."""
    row = connection.exec_driver_sql(
        "SELECT data FROM documents WHERE collection='workflow_version' AND id=?",
        (doc_id,),
    ).fetchone()
    document = json.loads(row[0])
    stamp = document.get("updated_at") or document.get("created_at")
    if not stamp:
        raise ValueError(f"version {doc_id} carries no timestamp to date its guide by")
    return str(stamp)
