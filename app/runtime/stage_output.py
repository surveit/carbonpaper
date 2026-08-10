"""What a stage handler returns: its rows, plus the two things that used to ride
the frame's `.attrs`. Fields, not a side channel — `.attrs` did not survive a
frame being rebuilt, so an attachment made before the last rebuild was silently
lost, and it cannot reach a parquet either.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import pandas as pd

from app.models.run_manifest import StageContribution

from .lineage import RowLineage


@dataclass(frozen=True)
class StageOutput:
    frame: pd.DataFrame
    # What this stage owes the manifest — token usage, per-row errors, dropped
    # columns, queue tallies. Empty rather than None: every reader merges it
    # unconditionally instead of testing for absence first.
    contribution: StageContribution = field(default_factory=StageContribution)
    # Which input rows each output row came from, where the handler's shape
    # knows it (a filter, a join, an aggregate). None means "not reported",
    # which is distinct from an empty lineage claiming no row had a parent.
    lineage: RowLineage | None = None

    def with_frame(self, frame: pd.DataFrame) -> "StageOutput":
        """The same contribution and lineage over a rebuilt frame."""
        return replace(self, frame=frame)
