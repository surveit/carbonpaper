"""a publish stage no longer stores a `template`

Revision ID: 0012
Revises: 0011
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from scripts.publish_template import move_publish_template_to_notes

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# The markup a publish stage emits moved into its own `function.code`, and
# PublishConfig forbids extras, so a stored spec still carrying `template` loads
# nowhere — 26 stored versions across three projects, and five compiled files.
#
# The compiled stage files under <project>/compiled/ hold the same specs and no
# revision reaches them — run `python -m scripts.migrate_compiled_stage_files
# --apply` alongside this, or those projects stop loading.
_COLLECTIONS = ("workflow_version", "draft")
_SCHEMA_VERSION = 5


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _retire_document_templates(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), _SCHEMA_VERSION, collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0012 is not reversible: the field's content is kept as a compiler_note, "
        "which a human may then edit or fold into function.code, so there is no "
        "text a downgrade could put back with confidence it is the one that left"
    )


def _retire_document_templates(document: Any) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    moved = [move_publish_template_to_notes(stage)
             for stage in stages if isinstance(stage, dict)]
    return any(moved)
