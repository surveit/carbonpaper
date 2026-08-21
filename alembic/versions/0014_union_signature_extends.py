"""a union's signature declares nothing, so its stored `produces` goes

Revision ID: 0014
Revises: 0013
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# A union concatenates rows across inputs it already forces to share one schema, so
# its output IS that schema and `produces` only ever restated it. UnionStage now takes
# an ExtendsSignature with reads/adds/rewrites all empty, and forbids extras, so a
# stored `replaces` signature loads nowhere.
_COLLECTIONS = ("workflow_version", "working_copy", "draft")
_SCHEMA_VERSION = 6


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _rewrite_union_signatures(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), _SCHEMA_VERSION, collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0014 is not reversible: `produces` restated the shared input schema, which "
        "is a function of the whole graph rather than of the union — recovering it "
        "means resolving every upstream stage again, and a union whose inputs have "
        "since changed would come back with a schema it never stored"
    )


def _rewrite_union_signatures(document: Any) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    # A list, not a generator: `any` short-circuits, and a document may hold
    # several unions.
    rewritten = [_rewrite_one(stage) for stage in stages if isinstance(stage, dict)]
    return any(rewritten)


def _rewrite_one(stage: dict[str, Any]) -> bool:
    if stage.get("type") != "union":
        return False
    signature = stage.get("signature")
    if not isinstance(signature, dict) or signature.get("form") != "replaces":
        return False
    stage["signature"] = {"form": "extends", "reads": [], "adds": [], "rewrites": []}
    return True
