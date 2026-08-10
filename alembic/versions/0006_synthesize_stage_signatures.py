"""synthesize a signature on every stored stage and drop its output_schema

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from scripts.stage_signatures import SignatureUndeterminable, add_signature

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# A stage's output schema now resolves from its `signature` alone, and the outer
# is gone from the model, so a payload carrying one no longer loads. The
# synthesis is shared with scripts.migrate_compiled_stage_files — a project's
# working copy carries the same specs and no revision can reach it.
#
# add_signature RAISES (SignatureUndeterminable) on a stage whose outer dropped
# an input column: an `extends` signature cannot express a drop, so the payload
# does not determine one and a human must author it.
_COLLECTIONS = ("workflow_version", "draft")


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            try:
                changed = _add_signatures(document)
            except SignatureUndeterminable as exc:
                # Refuse the RECORD, not the run: this revision migrates what the
                # stored payload determines, and 0007 carries the human decision
                # about the rest. The document is left exactly as it was.
                print(f"0006: left unmigrated — {doc_id}: {exc}")
                continue
            if not changed:
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )


def downgrade() -> None:
    # A signature records reads, which no stored outer ever carried; reversing
    # would have to invent them.
    raise NotImplementedError("0006 is not reversible: a signature records reads")


def _add_signatures(document: Any) -> bool:
    """Give every stage in `document` its signature; True if any payload changed."""
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    # A list, not a generator: `any` short-circuits, so a generator would stop
    # calling add_signature at the first stage it changed and leave the rest of
    # the document carrying an output_schema no model still loads.
    changed = [add_signature(stage) for stage in stages if isinstance(stage, dict)]
    return any(changed)
