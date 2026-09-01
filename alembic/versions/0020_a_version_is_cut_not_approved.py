"""drop `reviewer` and the publish fields from every stored workflow version

Revision ID: 0020
Revises: 0019
"""
from __future__ import annotations

import json

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

# PersistedModel forbids extra keys, so a stored version still carrying these
# would fail to load rather than ignore them.
_DROPPED = ("reviewer", "published", "published_at", "published_by")


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    for record_id, data in rows:
        payload = json.loads(data)
        if not any(field in payload for field in _DROPPED):
            continue
        for field in _DROPPED:
            payload.pop(field, None)
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection='workflow_version' AND id=?",
            (json.dumps(payload), record_id),
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    for record_id, data in rows:
        payload = json.loads(data)
        if "reviewer" in payload:
            continue
        # Who cut it was not recorded per-version before; "unknown" is the honest
        # value, and the publish flags return at their old defaults.
        payload["reviewer"] = "unknown"
        payload["published"] = False
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection='workflow_version' AND id=?",
            (json.dumps(payload), record_id),
        )
