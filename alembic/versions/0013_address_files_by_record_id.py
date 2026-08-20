"""stored file bytes are addressed by record id, not by content hash

Revision ID: 0013
Revises: 0012
"""
from __future__ import annotations

import json

from alembic import op

from app.core.files import files_root
from scripts.uploaded_file_addresses import StoredFile, move_store_to_record_addresses

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# The first revision here that touches the filesystem; every earlier one rewrites JSON in
# `documents`. No row changes at all — `sha256` stays a field on the record, it just stops
# naming the directory. Only the bytes move, from <files root>/<sha256>/<the name they
# were written under> to <files root>/<record id>/<the record's filename>.
_COLLECTION = "uploaded_file"


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT data FROM documents WHERE collection=?", (_COLLECTION,)
    ).fetchall()
    records = [StoredFile.model_validate(json.loads(data)) for (data,) in rows]
    move_store_to_record_addresses(files_root(), records)


def downgrade() -> None:
    raise NotImplementedError(
        "0013 is not reversible: content addressing kept one copy where each record now "
        "owns its own, and nothing on disk says which record's filename the shared "
        "directory was written under — going back would have to pick one and drop the "
        "rest of those records' bytes"
    )
