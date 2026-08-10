"""python_frame_function becomes pandas_frame_function

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import json

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# `Stage` is a discriminated union on `type`, and pydantic resolves the
# discriminator BEFORE any enum coercion, so `StageType._missing_` cannot rescue
# a stored spec the way it rescues a run manifest. Every stored spec has to carry
# the new string or it stops loading — hence this revision.
#
# It does NOT reach a project's compiled/ working copy, which is on disk:
# `python -m scripts.migrate_compiled_stage_files --apply` is that half. Skipping
# it leaves those projects unloadable, which has happened before.
_COLLECTIONS = ("workflow_version", "draft")
_OLD = "python_frame_function"
_NEW = "pandas_frame_function"


def upgrade() -> None:
    _rewrite_stage_types(_OLD, _NEW)


def downgrade() -> None:
    _rewrite_stage_types(_NEW, _OLD)


def _rewrite_stage_types(old: str, new: str) -> None:
    connection = op.get_bind()
    renamed = 0
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            changed = _rename_in_document(document, old, new)
            if not changed:
                continue
            renamed += changed
            connection.exec_driver_sql(
                "UPDATE documents SET data=? WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
    print(f"0011: renamed {renamed} stage(s) {old} -> {new}")


# Only a `type` key is rewritten. A stage's own `code`/`summary` may mention the
# name in prose, and that prose is the user's, not ours to edit.
def _rename_in_document(document: object, old: str, new: str) -> int:
    if isinstance(document, dict):
        renamed = 0
        for key, value in document.items():
            if key == "type" and value == old:
                document[key] = new
                renamed += 1
            else:
                renamed += _rename_in_document(value, old, new)
        return renamed
    if isinstance(document, list):
        return sum(_rename_in_document(item, old, new) for item in document)
    return 0
