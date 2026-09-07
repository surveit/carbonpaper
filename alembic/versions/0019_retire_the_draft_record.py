"""kept as a no-op: the draft collection it dropped is in use again

Revision ID: 0019
Revises: 0018
"""
from __future__ import annotations

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This deleted every `draft` row when the record was retired. The record came
    # back, so replaying the delete would destroy drafts people are editing.
    pass


def downgrade() -> None:
    pass
