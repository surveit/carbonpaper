"""a workflow version no longer carries approval coverage

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import json

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Node approval is gone, and with it the per-version `coverage` counts frozen at
# creation time. WorkflowVersion forbids extras, so a stored payload still
# carrying the key loads nowhere — strip it. The stage specs nested under
# `stages` are untouched, so schema_version does not move.
_COLLECTION = "workflow_version"
_DROPPED_KEY = "coverage"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not isinstance(document, dict) or _DROPPED_KEY not in document:
            continue
        del document[_DROPPED_KEY]
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection=? AND id=?",
            (json.dumps(document), _COLLECTION, str(doc_id)),
        )


def downgrade() -> None:
    raise NotImplementedError(
        "0009 is not reversible: the decision store the counts were computed from "
        "is gone, so no coverage can be recomputed for a version"
    )
