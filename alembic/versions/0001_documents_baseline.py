"""documents baseline

Revision ID: 0001
Revises:
"""
from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# Matches SqliteKvStore.__init__'s own CREATE TABLE IF NOT EXISTS verbatim. The
# store still creates the table when it opens a fresh database, so this revision
# is a baseline marker for an existing one, not the only way the table appears.
_CREATE_DOCUMENTS = (
    "CREATE TABLE IF NOT EXISTS documents ("
    "  collection TEXT NOT NULL,"
    "  id TEXT NOT NULL,"
    "  data TEXT NOT NULL,"
    "  schema_version INTEGER NOT NULL DEFAULT 1,"
    "  PRIMARY KEY (collection, id))"
)


def upgrade() -> None:
    op.execute(_CREATE_DOCUMENTS)


def downgrade() -> None:
    # Deliberately not dropping `documents` — the whole store lives in it.
    pass
