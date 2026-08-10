"""the data model leaves schemas/ for the store

Revision ID: 0010
Revises: 0009
"""
from __future__ import annotations

import json
from typing import Any

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# A project's named schemas are now one "data_model" document keyed by project
# name, and a version's frozen `schemas` are typed `NamedSchema` rather than raw
# dicts. The old on-disk reader injected `_filename` onto every schema it read,
# and those keys were frozen into each stored version — NamedSchema forbids
# extras, so a version still carrying them loads nowhere.
#
# This revision strips them. The schemas still sitting in <project>/schemas/ are
# not reachable from here — run `python -m tools.import_disk_schemas --apply` to
# bring those into the store.
_BOOKKEEPING_PREFIX = "_"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT id, data FROM documents WHERE collection='workflow_version'"
    ).fetchall()
    for doc_id, data in rows:
        document = json.loads(data)
        if not _strip_bookkeeping(document):
            continue
        connection.exec_driver_sql(
            "UPDATE documents SET data=? WHERE collection='workflow_version' AND id=?",
            (json.dumps(document), str(doc_id)),
        )


def _strip_bookkeeping(document: dict[str, Any]) -> bool:
    """Drop every `_`-prefixed key from the version's frozen schemas, reporting
    whether anything changed."""
    schemas = document.get("schemas")
    if not isinstance(schemas, list):
        return False
    changed = False
    for schema in schemas:
        if not isinstance(schema, dict):
            continue
        for key in [k for k in schema if k.startswith(_BOOKKEEPING_PREFIX)]:
            del schema[key]
            changed = True
    return changed


def downgrade() -> None:
    raise NotImplementedError("0010 is not reversible: the stripped keys are gone")
