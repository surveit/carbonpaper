"""the `publish` stage type, and its config block, become `report`

Revision ID: 0017
Revises: 0016
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

WAS = "publish"
NOW = "report"

# Stage specs: the type discriminator and the config block travel together, because
# StageDraft forbids extras and the discriminated union has no `publish` member left.
_SPEC_COLLECTIONS = ("workflow_version", "working_copy", "draft")
# A run's own record names the type it executed, on `stage_records[].type`. Left
# behind it parses nowhere, so every stored run would stop loading.
_RUN_COLLECTION = "run"
# 4 was what a live save stamped while 0012 stamped 5 and 0014 stamped 6, so a
# migrated row and a freshly written one disagreed. One counter from here.
_SCHEMA_VERSION = 7


def upgrade() -> None:
    _rewrite(_SPEC_COLLECTIONS, _rename_stage_specs, WAS, NOW, _SCHEMA_VERSION)
    _rewrite((_RUN_COLLECTION,), _rename_stage_records, WAS, NOW, None)


def downgrade() -> None:
    _rewrite(_SPEC_COLLECTIONS, _rename_stage_specs, NOW, WAS, 6)
    _rewrite((_RUN_COLLECTION,), _rename_stage_records, NOW, WAS, None)


def _rewrite(
    collections: tuple[str, ...],
    rename: Any,
    was: str,
    now: str,
    schema_version: int | None,
) -> None:
    connection = op.get_bind()
    for collection in collections:
        rows = connection.exec_driver_sql(
            "SELECT id, data FROM documents WHERE collection=?", (collection,)
        ).fetchall()
        for doc_id, data in rows:
            document = json.loads(data)
            if not rename(document, was, now):
                continue
            if schema_version is None:
                connection.exec_driver_sql(
                    "UPDATE documents SET data=? WHERE collection=? AND id=?",
                    (json.dumps(document), collection, str(doc_id)),
                )
                continue
            connection.exec_driver_sql(
                "UPDATE documents SET data=?, schema_version=? "
                "WHERE collection=? AND id=?",
                (json.dumps(document), schema_version, collection, str(doc_id)),
            )


def _rename_stage_specs(document: Any, was: str, now: str) -> bool:
    return _rename_each(document, "stages", _rename_one_spec, was, now)


def _rename_stage_records(document: Any, was: str, now: str) -> bool:
    return _rename_each(document, "stage_records", _rename_one_type, was, now)


def _rename_each(document: Any, key: str, rename: Any, was: str, now: str) -> bool:
    entries = document.get(key) if isinstance(document, dict) else None
    if not isinstance(entries, list):
        return False
    # A list, not a generator: `any` short-circuits, and a document may hold several.
    renamed = [rename(e, was, now) for e in entries if isinstance(e, dict)]
    return any(renamed)


def _rename_one_spec(stage: dict[str, Any], was: str, now: str) -> bool:
    if not _rename_one_type(stage, was, now):
        return False
    if was in stage:
        stage[now] = stage.pop(was)
    return True


def _rename_one_type(stage: dict[str, Any], was: str, now: str) -> bool:
    if stage.get("type") != was:
        return False
    stage["type"] = now
    return True
