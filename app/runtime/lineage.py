"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union), worked out by the RUNTIME, never reported by the stage:
the row driver knows which input ordinals it emitted, and a union's inputs
already say by their lengths. It rides the output frame's `.attrs` rather than
columns on it, so no runtime machinery can reach a stage's real output."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from app.models.stage import Stage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"

# The `.attrs` channel the row driver hands lineage out on. The executor reads
# it BEFORE any row slicing and pops it before persisting — `.attrs` does not
# survive parquet. The row-grain cache sits below this: a row replayed from
# cache fills its slot exactly as a computed one does, so the two never
# interact.
LINEAGE_ATTR = "row_lineage"


@dataclass(frozen=True)
class RowLineage:
    """Where each of a stage's own output rows came from: parallel lists naming,
    per output row in output order, the input stage id and the row ordinal
    within that stage's output."""

    source_stage: list[str]
    source_row: list[int]

    def __post_init__(self) -> None:
        if len(self.source_stage) != len(self.source_row):
            raise ValueError("row lineage needs one source stage per source row")

    def shifted(self, offset: int) -> "RowLineage":
        """Ordinals counted from a sliced input frame's first row, moved onto the upstream's own."""
        if offset == 0:
            return self
        return RowLineage(list(self.source_stage), [r + offset for r in self.source_row])

    def to_frame(self) -> pd.DataFrame:
        """The sidecar frame, one row per output row, in output order."""
        return pd.DataFrame({
            TRACE_SOURCE_STAGE_KEY: self.source_stage,
            TRACE_SOURCE_ROW_KEY: self.source_row,
        })


def attach_row_lineage(df: pd.DataFrame, lineage: RowLineage) -> pd.DataFrame:
    """Hand `lineage` out on `df`'s `.attrs` for the executor to pick up. Set on
    the frame the handler actually returns, since `.attrs` does not survive a
    frame being rebuilt."""
    df.attrs[LINEAGE_ATTR] = lineage
    return df


def read_row_lineage(df: pd.DataFrame | None) -> RowLineage | None:
    """The lineage a handler attached, or None where none rode along."""
    if df is None:
        return None
    attached = df.attrs.get(LINEAGE_ATTR)
    return attached if isinstance(attached, RowLineage) else None


def kept_rows_lineage(source_stage_id: str, kept_indices: list[int]) -> RowLineage:
    """Lineage for a stage that emitted a subsequence of ONE input's rows —
    `kept_indices` being the input ordinals it kept, in output order."""
    return RowLineage([source_stage_id] * len(kept_indices), list(kept_indices))


def concatenated_inputs_lineage(
    stage: "Stage", inputs: dict[str, pd.DataFrame], first_row_ordinal: int = 0
) -> RowLineage:
    """Lineage for a stage that emitted its inputs concatenated in declared
    order (union), computed from their row counts alone — the runtime knows the
    lengths it handed over, so the stage is not consulted.

    `inputs` are the frames the handler was GIVEN, so where the runtime sliced
    them the first one is the upstream's row `first_row_ordinal`, not its row 0."""
    source_stage: list[str] = []
    source_row: list[int] = []
    for ref in stage.inputs:
        rows = len(inputs[ref.id])
        source_stage.extend([ref.id] * rows)
        source_row.extend(range(first_row_ordinal, first_row_ordinal + rows))
    return RowLineage(source_stage, source_row)


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    """Where a stage's row-provenance sidecar lives, alongside its own output
    parquet in the run directory."""
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"
