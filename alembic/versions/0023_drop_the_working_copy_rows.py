"""delete every `working_copy` row: the record is gone and nothing reads the collection

Revision ID: 0023
Revises: 0022
"""
from __future__ import annotations

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.get_bind().exec_driver_sql("DELETE FROM documents WHERE collection='working_copy'")


def downgrade() -> None:
    # The rows are gone; a project's stages are its newest version either way.
    pass
