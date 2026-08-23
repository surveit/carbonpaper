"""What a stage handler returns: its rows as an arrow table, plus the two things
that used to ride the frame's `.attrs`. Arrow is the wire format; pandas is
materialized only where authored code reads a frame.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd
import pyarrow as pa

from app.core.frames import frame_to_table
from app.models.stage_contribution import StageContribution

from .branches import RowBranches
from .lineage import RowLineage


@dataclass(frozen=True)
class AwaitingReview:
    """A stage that queued rows for a person and cannot finish until they decide."""

    stage_id: str
    pending_count: int
    queue_path: Path


@dataclass(frozen=True)
class StageOutput:
    table: pa.Table
    # Empty, never None: every reader merges it unconditionally, skipping an absence check.
    contribution: StageContribution = field(default_factory=StageContribution)
    # Which input rows each output row came from, where the handler's shape
    # knows it (a filter, a join, an aggregate). None means "not reported",
    # which is distinct from an empty lineage claiming no row had a parent.
    lineage: RowLineage | None = None
    # Which branch of its code each row took; None where nothing ran.
    branches: RowBranches | None = None
    # Set where the stage queued rows for review; `table` is then not its output.
    awaiting_review: AwaitingReview | None = None

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
