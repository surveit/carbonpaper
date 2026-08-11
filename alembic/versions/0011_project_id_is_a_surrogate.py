"""A project is identified by a minted id, not by its name

Revision ID: 0011
Revises: 0010
"""
from __future__ import annotations

import json

from alembic import op

from app.core.timestamp_ids import mint_timestamp_id

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

# Until now a project record's store id WAS its name, which made the name an
# identifier: renaming was impossible, and a name could be held by a record whose
# directory was gone. The record now carries a minted `id` and a `name` field, so
# the name becomes a label.
#
# `name` is REQUIRED on the model, so every stored record must gain one or fail
# PersistedModel.load's extra="forbid" validation — that is what this revision is
# for. The value is never guessed: the old store id IS the name, so each record
# supplies its own. A row that cannot supply one is refused, not filled.
_COLLECTION = "project"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for old_id, data in rows:
        document = json.loads(data)
        _refuse_undeterminable(old_id, document)
        document["name"] = str(old_id)
        document["id"] = mint_timestamp_id()
        connection.exec_driver_sql(
            "UPDATE documents SET id=?, data=? WHERE collection=? AND id=?",
            (document["id"], json.dumps(document), _COLLECTION, str(old_id)),
        )


def downgrade() -> None:
    raise NotImplementedError(
        "0011 is not reversible: restoring the name as the store id would re-merge "
        "any two projects that have since been given the same name"
    )


def _refuse_undeterminable(old_id: object, document: object) -> None:
    if not isinstance(document, dict):
        raise ValueError(f"project record {old_id!r} is not a JSON object — cannot migrate")
    stored_name = document.get("name")
    if stored_name is not None and stored_name != str(old_id):
        raise ValueError(
            f"project record {old_id!r} already carries name {stored_name!r}, which "
            f"disagrees with its store id — a human must say which is the name"
        )
