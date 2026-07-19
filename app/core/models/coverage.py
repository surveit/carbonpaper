"""Coverage — approval coverage over a workflow's stages.

How many of a workflow's stages sit in each belief state (approved / rejected /
edited-stale / unreviewed), the total, and the approved percentage (over total;
0.0 when there are no stages). Computed by app.services.node_review.coverage_for
and frozen into a version's metadata."""
from __future__ import annotations

from pydantic import BaseModel


class Coverage(BaseModel):
    approved: int
    rejected: int
    edited_stale: int
    unreviewed: int
    total: int
    approved_pct: float
