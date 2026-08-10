"""filter_rows and human_review_queue declare what they read

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# 0006/0007 synthesized a signature for every stage, but two types were given
# writes and no `reads`: filter_rows got `{"form": "extends"}` and
# human_review_queue got its `adds` alone. Both run an authored expression over
# the row — a predicate, a queue `filter` — so like the other opaque-code types
# the whole anchor edge is what they may consume, and an empty `reads` understates
# it. The runtime now hands a row-mapped mapper only what its signature declares,
# so a stage of either type must say so or be handed nothing.
#
# Only a stage whose reads are EMPTY is touched. One that already names columns
# was authored or repaired deliberately and this revision must not widen it.
_COLLECTIONS = ("workflow_version", "draft")
_TYPES = ("filter_rows", "human_review_queue")


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _backfill_document(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0010 is not reversible: emptying `reads` again would restore a signature "
        "that understates what the stage consumes"
    )


def _backfill_document(document: Any) -> bool:
    """Give every filter_rows/queue stage its anchor reads; True if anything changed."""
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    changed = [_backfill_stage(stage) for stage in stages if isinstance(stage, dict)]
    return any(changed)


def _backfill_stage(stage: dict[str, Any]) -> bool:
    if stage.get("type") not in _TYPES:
        return False
    signature = stage.get("signature")
    if not isinstance(signature, dict) or signature.get("reads"):
        return False
    anchor = _anchor(stage)
    if anchor is None:
        return False
    anchor_id, columns = anchor
    if not columns:
        return False
    signature["reads"] = [{"input": anchor_id, "columns": columns}]
    return True


def _anchor(stage: dict[str, Any]) -> tuple[str, list[dict[str, Any]]] | None:
    """The first input's id and declared columns, or None when it declares none."""
    inputs = stage.get("inputs")
    if not isinstance(inputs, list) or not inputs or not isinstance(inputs[0], dict):
        return None
    anchor_id = inputs[0].get("id")
    schema = inputs[0].get("schema")
    if not isinstance(anchor_id, str) or not isinstance(schema, dict):
        return None
    columns = schema.get("columns")
    if not isinstance(columns, list):
        return None
    return anchor_id, [dict(column) for column in columns if isinstance(column, dict)]
