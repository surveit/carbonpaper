"""finish the signature synthesis 0006 short-circuited out of

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from tools.stage_signatures import add_signature

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# 0006 drove its per-stage synthesis through `any(...)` over a GENERATOR, which
# stops at the first stage it changed — so a store it upgraded carries stage 0
# migrated and every later stage still holding an output_schema no model loads,
# and every page that reads a version document 500s. 0006 is fixed in place for
# a store that has not run it; this revision repairs one that has.
#
# add_signature is idempotent (a stage already carrying a signature and no
# output_schema returns False untouched), so this re-runs the whole pass rather
# than trying to identify which stages 0006 reached.
_COLLECTIONS = ("workflow_version", "draft")


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _add_signatures(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )


def downgrade() -> None:
    # Same as 0006: a signature records reads no stored outer ever carried.
    raise NotImplementedError("0007 is not reversible: a signature records reads")


def _add_signatures(document: Any) -> bool:
    """Give every stage in `document` its signature; True if any payload changed."""
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    changed = [add_signature(stage) for stage in stages if isinstance(stage, dict)]
    return any(changed)
