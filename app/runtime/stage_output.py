"""What a stage handler returns: its rows as an arrow table, plus the two things
that used to ride the frame's `.attrs`. Arrow is the wire format; pandas is
materialized only where authored code reads a frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import pandas as pd
import pyarrow as pa

from app.core.frames import frame_to_table
from app.models.run_manifest import StageContribution

from .lineage import RowLineage


@dataclass(frozen=True)
class StageOutput:
    table: pa.Table
    # What this stage owes the manifest — token usage, per-row errors, dropped
    # columns, queue counts. Empty rather than None: every reader merges it
    # unconditionally instead of testing for absence first.
    contribution: StageContribution = field(default_factory=StageContribution)
    # Which input rows each output row came from, where the handler's shape
    # knows it (a filter, a join, an aggregate). None means "not reported",
    # which is distinct from an empty lineage claiming no row had a parent.
    lineage: RowLineage | None = None

    @classmethod
    def from_frame(
        cls,
        frame: pd.DataFrame,
        contribution: StageContribution | None = None,
        lineage: RowLineage | None = None,
    ) -> "StageOutput":
        """For a handler that materialized pandas to run authored code — it coerces back here."""
        return cls(
            frame_to_table(frame),
            contribution if contribution is not None else StageContribution(),
            lineage,
        )

    def with_table(self, table: pa.Table) -> "StageOutput":
        """The same contribution and lineage over a rebuilt table."""
        return replace(self, table=table)
