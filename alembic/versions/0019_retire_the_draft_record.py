"""drop the `draft` collection: its service had no caller and its rows held no unsaved work

Revision ID: 0019
Revises: 0018
"""
from __future__ import annotations

from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("DELETE FROM documents WHERE collection='draft'")


def downgrade() -> None:
    # The rows are gone; a downgrade restores the empty collection, not its contents.
    pass
