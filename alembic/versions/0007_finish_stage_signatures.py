"""finish the signature synthesis 0006 short-circuited out of

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from tools.stage_signatures import add_signature, find_dropped_anchor_columns

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
#
# WIDENING, by explicit human decision: a stage whose stored outer dropped anchor
# columns cannot be expressed as `extends`, and the payload does not determine
# what was meant. Rather than refuse, this revision lets those columns FLOW —
# the stage now emits columns its stored spec said it did not. Every widened
# stage is printed with the columns it gained, because nothing else records it.
_COLLECTIONS = ("workflow_version", "draft")


def upgrade() -> None:
    connection = op.get_bind()
    widened: list[str] = []
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _add_signatures(document, str(doc_id), widened):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
    _report(widened)


def _report(widened: list[str]) -> None:
    if not widened:
        print("0007: no stage widened; every signature was determinable")
        return
    print(f"0007: {len(widened)} stage(s) WIDENED — each now emits columns its "
          f"stored output_schema dropped:")
    for line in widened:
        print(f"  {line}")


def downgrade() -> None:
    # Same as 0006: a signature records reads no stored outer ever carried.
    raise NotImplementedError("0007 is not reversible: a signature records reads")


def _add_signatures(document: Any, doc_id: str, widened: list[str]) -> bool:
    """Give every stage in `document` its signature; True if any payload changed."""
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    changed = [_add_one(stage, doc_id, widened)
               for stage in stages if isinstance(stage, dict)]
    return any(changed)


def _add_one(stage: dict[str, Any], doc_id: str, widened: list[str]) -> bool:
    dropped = find_dropped_anchor_columns(stage)
    if dropped:
        widened.append(f"{doc_id} :: {stage.get('id')} ({stage.get('type')}) "
                       f"regains {dropped}")
    return add_signature(stage, allow_drops=True)
