"""a stage's code is inline; `function.kind=module` is gone

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from alembic import op

from app.services.workspace import configure_projects_dir_from_env, projects_dir
from scripts.module_function_code import inline_module_function

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# A stage's code now lives on the stage. Nothing can put a module where a running
# server would import it, and the one project that used this stopped working when
# the store moved out of the checkout and its `examples.` package stopped existing.
# The code itself is intact under the projects root, so it moves onto the spec.
_COLLECTIONS = ("workflow_version", "draft", "working_copy")
_SCHEMA_VERSION = 6


def upgrade() -> None:
    configure_projects_dir_from_env()
    projects_root = projects_dir()
    connection = op.get_bind()
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not _inline_document_modules(document, projects_root):
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), _SCHEMA_VERSION, collection, str(doc_id)),
            )


def downgrade() -> None:
    raise NotImplementedError(
        "0013 is not reversible: the module path a spec pointed at is not recoverable "
        "from the code that replaced it, and no running server could import it back"
    )


# A store holding no such project holds no such document either, so there is nothing
# here to migrate and nothing to tolerate — a document that IS here and cannot be
# resolved stops the migration rather than storing an empty `code`.
def _inline_document_modules(document: Any, projects_root: Path) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    inlined = [inline_module_function(stage, projects_root)
               for stage in stages if isinstance(stage, dict)]
    return any(inlined)
