"""a stage no longer stores the schema of each of its inputs

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from scripts.stage_input_schemas import drop_stored_input_schemas

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# A stage's input schemas are a function of the whole graph, not of the stage, so
# app.models.workflow.Workflow resolves them and hands them out as WorkflowStage.
# StageInput is an id alone and forbids extras, so a payload still carrying
# `schema` loads nowhere.
#
# The compiled stage files under <project>/compiled/ hold the same specs and no
# revision reaches them — run `python -m scripts.migrate_compiled_stage_files
# --apply` alongside this, or those projects stop loading.
_COLLECTIONS = ("workflow_version", "draft")
_SCHEMA_VERSION = 4


def upgrade() -> None:
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _drop_document_input_schemas(document):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), _SCHEMA_VERSION, collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0011 is not reversible: the stored schema was what a stage REQUIRED of an "
        "input, checked as a subset of the upstream output, so resolving the graph "
        "again recovers the upstream output rather than what was written — 172 of "
        "the 553 resolvable stored refs named fewer columns than resolution computes"
    )


def _drop_document_input_schemas(document: Any) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    dropped = [drop_stored_input_schemas(stage)
               for stage in stages if isinstance(stage, dict)]
    return any(dropped)
