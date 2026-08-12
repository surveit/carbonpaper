"""a stage's `name` becomes its `description`

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

from scripts.stage_description import (
    DescriptionUndeterminable,
    rename_name_to_description,
)

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# A stage now has ONE name — its id — shown on every surface, and `description` is
# the line under it. The stored key moves with the model: AuthoredStageFields forbids
# extras, so a payload still spelling it `name` loads nowhere.
#
# The compiled stage files under <project>/compiled/ hold the same specs and no
# revision reaches them — run `python -m scripts.migrate_compiled_stage_files
# --apply` alongside this, or those projects stop loading.
_COLLECTIONS = ("workflow_version", "draft")


def upgrade() -> None:
    connection = op.get_bind()
    refused: list[str] = []
    for collection in _COLLECTIONS:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            try:
                changed = _rename_stages(document)
            except DescriptionUndeterminable as exc:
                # Refuse the RECORD, not the run: one document a human must decide
                # must not hold back every other project's migration.
                refused.append(f"{doc_id}: {exc}")
                continue
            if not changed:
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=3 "
                "WHERE collection=? AND id=?",
                (json.dumps(document), collection, str(doc_id)),
            )
    _report(refused)


def _report(refused: list[str]) -> None:
    if refused:
        print(f"0008: {len(refused)} document(s) REFUSED and left unmigrated — a human "
              f"must resolve these before they will load:")
        for line in refused:
            print(f"  {line}")


def downgrade() -> None:
    raise NotImplementedError(
        "0008 is not reversible: a description written under today's limit need not "
        "fit anything the old `name` promised"
    )


def _rename_stages(document: Any) -> bool:
    stages = document.get("stages") if isinstance(document, dict) else None
    if not isinstance(stages, list):
        return False
    renamed = [rename_name_to_description(stage)
               for stage in stages if isinstance(stage, dict)]
    return any(renamed)
