"""Approval coverage over a set of workflow stages."""
from __future__ import annotations

from pydantic import BaseModel


class Coverage(BaseModel):
    """Approval coverage over a workflow's stages (mirrors
    app.services.node_review.coverage_for): how many stages sit in each belief
    state, the total, and the approved percentage (over total; 0.0 when there
    are no stages)."""

    approved: int
    rejected: int
    edited_stale: int
    unreviewed: int
    total: int
    approved_pct: float
