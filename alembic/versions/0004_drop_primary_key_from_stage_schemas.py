"""drop primary_key from every stage schema

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# `primary_key` left the stage vocabulary and TableSchema forbids extras, so every
# stored schema still carrying it is unreadable. The key is dropped wherever it
# appears — a stage's own output_schema and each declared input's schema.
_KEY = "primary_key"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not _drop_primary_keys(document):
            continue
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection='workflow_version' AND id=?",
            (json.dumps(document), str(doc_id)),
        )


def downgrade() -> None:
    # The stage vocabulary no longer has the concept, so there is no value to put back.
    raise NotImplementedError("0004 is not reversible: primary_key left the vocabulary")


def _drop_primary_keys(node: Any) -> bool:
    """Remove every `primary_key` key anywhere in the document; True if any was found.

    Walks rather than addressing known paths: a schema hangs off a stage's
    output_schema and off each input, and a stage type added later would put one
    somewhere this revision cannot know about.
    """
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
