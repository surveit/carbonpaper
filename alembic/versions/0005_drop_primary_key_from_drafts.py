"""drop primary_key from every draft's stage schemas

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# 0004 did this for `workflow_version` while a read-side upgrade still covered
# `draft`. With that upgrade gone, the same key has to come out of the drafts too
# or a draft written before the vocabulary change no longer loads.
_KEY = "primary_key"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='draft'"
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not _drop_primary_keys(document):
            continue
        connection.exec_driver_sql(
            "UPDATE documents SET data=?, schema_version=2 "
            "WHERE collection='draft' AND id=?",
            (json.dumps(document), str(doc_id)),
        )


def downgrade() -> None:
    # The stage vocabulary no longer has the concept, so there is no value to put back.
    raise NotImplementedError("0005 is not reversible: primary_key left the vocabulary")


def _drop_primary_keys(node: Any) -> bool:
    """Remove every `primary_key` key anywhere in the document; True if any was found."""
    found = False
    if isinstance(node, dict):
        found = node.pop(_KEY, _MISSING) is not _MISSING
        for value in node.values():
            found |= _drop_primary_keys(value)
    elif isinstance(node, list):
        for item in node:
            found |= _drop_primary_keys(item)
    return found


_MISSING = object()
