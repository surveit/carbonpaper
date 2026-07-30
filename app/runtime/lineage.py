"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME, never reported by the
authored stage. It rides the output frame's `.attrs` rather than columns on it,
so no runtime machinery can reach a stage's real output. A row may have several
parents, so the sidecar is list-valued — see `RowLineage`."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from app.models.stage import Stage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"
TRACE_EDGE_KIND_KEY = "_trace_edge_kind"

# The `.attrs` channel the row driver hands lineage out on. The executor reads
# it BEFORE any row slicing and pops it before persisting — `.attrs` does not
# survive parquet. The row-grain cache sits below this: a row replayed from
# cache fills its slot exactly as a computed one does, so the two never
# interact.
LINEAGE_ATTR = "row_lineage"


class EdgeKind(str, Enum):
    """How a parent relates to the row it produced."""

    # Bounded per row, so the tracer walks these.
    derivation = "derivation"
    # Unbounded per row (an aggregate's contributors): a cohort to open, never
    # a step to take, so the walk reports these rather than following them.
    contribution = "contribution"
    # Bounded but not pinned to individual rows. Recorded so the walk can say
    # what it could not determine instead of stopping dead.
    unresolved = "unresolved"


@dataclass(frozen=True)
class RowParent:
    """One input row that fed an output row."""

    stage_id: str
    row_ordinal: int
    kind: str = EdgeKind.derivation.value


@dataclass(frozen=True)
class RowLineage:
    """Entry i is the list of parents of output row i, in output order."""

    # Spine first, then branches. A parent ABSENT from a row's list records a
    # NON-MATCH — the one thing that tells "no matching row existed" apart from
    # "matched a row whose columns are null", which nulls alone cannot carry.
    parents: list[list[RowParent]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for entry in self.parents:
            if not isinstance(entry, list):
                raise ValueError("row lineage needs a list of parents per output row")

    def __len__(self) -> int:
        return len(self.parents)

    def shifted(self, offset: int) -> "RowLineage":
        """Ordinals counted from a sliced input frame's first row, moved onto the upstream's own."""
        # ONE offset covers every parent of every row: the runtime cuts the same
        # window out of each of a stage's inputs, so a join's two sides shift together.
        if offset == 0:
            return self
        return RowLineage([
            [RowParent(p.stage_id, p.row_ordinal + offset, p.kind) for p in entry]
            for entry in self.parents
        ])

    def to_frame(self) -> pd.DataFrame:
        """One row per output row; the three columns are parallel per-parent lists."""
        return pd.DataFrame({
            TRACE_SOURCE_STAGE_KEY: pd.Series(
                [[p.stage_id for p in entry] for entry in self.parents], dtype=object),
            TRACE_SOURCE_ROW_KEY: pd.Series(
                [[p.row_ordinal for p in entry] for entry in self.parents], dtype=object),
            TRACE_EDGE_KIND_KEY: pd.Series(
                [[str(p.kind) for p in entry] for entry in self.parents], dtype=object),
        })

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "RowLineage":
        """Read a sidecar frame back, including one written before lineage went multi-parent."""
        # A pre-multi-parent sidecar held SCALARS and no kind column, so each of
        # its rows reads as one derivation parent — old runs stay traceable
        # without a migration.
        has_kind = TRACE_EDGE_KIND_KEY in df.columns
        parents: list[list[RowParent]] = []
        for i in range(len(df)):
            stages = _as_list(df[TRACE_SOURCE_STAGE_KEY].iloc[i])
            rows = _as_list(df[TRACE_SOURCE_ROW_KEY].iloc[i])
            kinds = _as_list(df[TRACE_EDGE_KIND_KEY].iloc[i]) if has_kind else []
            entry = [
                RowParent(
                    stage_id=str(stages[k]),
                    row_ordinal=int(rows[k]),
                    kind=str(kinds[k]) if k < len(kinds) else EdgeKind.derivation.value,
                )
                for k in range(min(len(stages), len(rows)))
            ]
            parents.append(entry)
        return cls(parents)


def _as_list(value: Any) -> list[Any]:
    """A sidecar cell as a plain list: array, scalar and null all normalize here."""
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    return [value]


def attach_row_lineage(df: pd.DataFrame, lineage: RowLineage) -> pd.DataFrame:
    """Hand `lineage` out on `.attrs`; call it on the frame the handler RETURNS."""
    # `.attrs` does not survive a frame being rebuilt, so an earlier call is lost.
    df.attrs[LINEAGE_ATTR] = lineage
    return df


def read_row_lineage(df: pd.DataFrame | None) -> RowLineage | None:
    """The lineage a handler attached, or None where none rode along."""
    if df is None:
        return None
    attached = df.attrs.get(LINEAGE_ATTR)
    return attached if isinstance(attached, RowLineage) else None


def single_parent_lineage(
    source_stage_id: str, source_rows: Iterable[int]
) -> RowLineage:
    """One parent per output row: `source_rows` are that input's ordinals, in output order."""
    return RowLineage([
        [RowParent(source_stage_id, int(r))] for r in source_rows
    ])


def kept_rows_lineage(source_stage_id: str, kept_indices: list[int]) -> RowLineage:
    """For a stage that emitted a subsequence of one input's rows (filter_rows)."""
    return single_parent_lineage(source_stage_id, kept_indices)


def concatenated_inputs_lineage(
    stage: "Stage", inputs: dict[str, pd.DataFrame], first_row_ordinal: int = 0
) -> RowLineage:
    """For a union: derived from input row counts alone, so the stage is not consulted."""
    # `inputs` are the frames the handler was GIVEN, so where the runtime sliced
    # them the first row is the upstream's `first_row_ordinal`, not its row 0.
    parents: list[list[RowParent]] = []
    for ref in stage.inputs:
        rows = len(inputs[ref.id])
        parents.extend(
            [RowParent(ref.id, r)]
            for r in range(first_row_ordinal, first_row_ordinal + rows)
        )
    return RowLineage(parents)


def paired_inputs_lineage(
    left_stage_id: str, left_rows: Iterable[Any],
    right_stage_id: str, right_rows: Iterable[Any],
) -> RowLineage:
    """For a join: one row from each of two inputs per output row, subject first."""
    # Ordinals arrive as read back off the merged frame, so an unmatched side is
    # NaN/None and is then simply absent from the row's parents — that absence IS
    # the recorded non-match. Subject first, so it becomes the spine; where only
    # the reference matched, it is the row's only parent and the spine follows it.
    parents: list[list[RowParent]] = []
    for left_ord, right_ord in zip(left_rows, right_rows):
        entry: list[RowParent] = []
        if not _is_missing(left_ord):
            entry.append(RowParent(left_stage_id, int(left_ord)))
        if not _is_missing(right_ord):
            entry.append(RowParent(right_stage_id, int(right_ord)))
        parents.append(entry)
    return RowLineage(parents)


def _is_missing(value: Any) -> bool:
    """True for an ordinal a merge left unmatched (NaN, None and pd.NA alike)."""
    return value is None or bool(pd.isna(value))


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    """Where a stage's sidecar lives, alongside its output parquet in the run dir."""
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"
