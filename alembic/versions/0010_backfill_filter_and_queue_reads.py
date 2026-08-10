"""filter_rows and human_review_queue declare what they read

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from scripts.stage_signatures import backfill_anchor_reads

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
# The rewrite itself is backfill_anchor_reads, shared with
# scripts.migrate_compiled_stage_files — a project's working copy carries the same
# specs and no revision can reach it.
_COLLECTIONS = ("workflow_version", "draft")


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
    changed = [backfill_anchor_reads(stage) for stage in stages if isinstance(stage, dict)]
    return any(changed)
