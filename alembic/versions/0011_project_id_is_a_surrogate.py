"""A project is identified by a minted id, not by its name

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

# A project record's id is the name of its directory under the projects root, and
# that is all it has ever been. What changes is what a NEW project's directory is
# called: a minted id rather than a slug of the title, so two projects may carry the
# same `name` without colliding on disk.
#
# Existing ids are therefore left exactly as they are — an older project's id is a
# readable slug and stays one, because it is still the directory it names. Only the
# new REQUIRED `name` field is added, and each record supplies its own value: the id
# it already carries is the name it was created under. A row that cannot supply one
# is refused, not filled.
_COLLECTION = "project"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        _refuse_undeterminable(doc_id, document)
        document["name"] = str(doc_id)
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection=? AND id=?",
            (json.dumps(document), _COLLECTION, str(doc_id)),
        )


def downgrade() -> None:
    raise NotImplementedError(
        "0011 is not reversible: dropping `name` would lose the label of every "
        "project renamed since, which is stored nowhere else"
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
