"""What a publish stage said about where a published value came from."""
from __future__ import annotations

from pydantic import BaseModel


class Citation(BaseModel):
    stage_id: str
    row_ordinal: int
    column: str
    label: str  # what the artifact calls this value
    value: str  # the cell, checked against the row before this was recorded


class CitedRow(BaseModel):
    # A row claimed with no value — a table row's show-the-work link.
    stage_id: str
    row_ordinal: int
