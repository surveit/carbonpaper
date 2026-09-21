"""a stage that decides which rows are kept carries a `predicate`

Revision ID: 0022
Revises: 0021
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from app.models.stages.predicates import PREDICATE_BLOCKS, PREDICATE_NOT_WRITTEN

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# `predicate` is now required on a filter's, a starlark filter's and a queue's config
# block, so a stored stage without one loads nowhere. Every such stage written before
# the field existed is filled with PREDICATE_NOT_WRITTEN, which everything that reads
# a predicate treats as unwritten — the `unsaid_test` compiler warning names them.
_COLLECTIONS = ("workflow_version", "working_copy")


def upgrade() -> None:
    _rewrite(_fill_the_predicate)


def downgrade() -> None:
    _rewrite(_drop_the_filler)


def _rewrite(change: Any) -> None:
    connection = op.get_bind()
    filled = 0
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            changed = change(document)
            if not changed:
                continue
            filled += changed
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
    print(f"{revision}: {filled} decision block(s) touched")


def _fill_the_predicate(document: Any) -> int:
    return _walk(document, lambda block: (
        0 if block.get("predicate")
        else (block.update(predicate=PREDICATE_NOT_WRITTEN) or 1)))


def _drop_the_filler(document: Any) -> int:
    return _walk(document, lambda block: (
        1 if block.pop("predicate", None) == PREDICATE_NOT_WRITTEN else 0))


def _walk(document: Any, change: Any) -> int:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return 0
    return sum(change(stage[holder])
               for stage in stages if isinstance(stage, dict)
               for holder in PREDICATE_BLOCKS
               if isinstance(stage.get(holder), dict))
