"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME, never reported by the
authored stage. It rides the output frame's `.attrs` rather than columns on it,
so no runtime machinery can reach a stage's real output. A row may have several
parents, so the sidecar is list-valued — see `RowLineage`."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from app.models.stage import Stage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"
TRACE_EDGE_KIND_KEY = "_trace_edge_kind"
TRACE_SUPPLIES_KEY = "_trace_supplies"
TRACE_SUPPLIES_TABLE_KEY = "_trace_supplies_table"

# The supplies index every parent that fed its output row WHOLE carries.
WHOLE_ROW = 0

# The `.attrs` channel the row driver hands lineage out on. The executor reads
# it BEFORE any row slicing and pops it before persisting — `.attrs` does not
# survive parquet. The row-grain cache sits below this: a row replayed from
# cache fills its slot exactly as a computed one does, so the two never
# interact.
LINEAGE_ATTR = "row_lineage"


class EdgeKind(str, Enum):
    """How a parent relates to the row it produced."""

    # An enrich's subject row, and the reference row merged into it.
    direct = "direct"
    # Every filing in the quarter an aggregate totalled into one row.
    contribution = "contribution"
    # A python_frame_function pivoted the frame: this input fed the output, but
    # which of its rows fed THIS row was not recoverable.
    unknown = "unknown"


@dataclass(frozen=True)
class RowParent:
    """One input row that fed an output row."""

    stage_id: str
    row_ordinal: int
    kind: str = EdgeKind.direct.value
    # Index into the owning RowLineage's `supplies` table, naming the output
    # columns this parent fed. Interned rather than held inline because column
    # attribution varies per INPUT ROLE and per `where`, never per row: a join's
    # two sides are two entries for the whole stage, and an aggregate's are one
    # per distinct filter. Inline it and an aggregate's sidecar carries a tag per
    # column per row for information that has a handful of distinct values.
    supplies: int = WHOLE_ROW


@dataclass(frozen=True)
class RowLineage:
    """Entry i is the list of parents of output row i, in output order."""

    # Spine first, then branches. A parent ABSENT from a row's list records a
    # NON-MATCH — the one thing that tells "no matching row existed" apart from
    # "matched a row whose columns are null", which nulls alone cannot carry.
    parents: list[list[RowParent]] = field(default_factory=list)
    # supplies[i] = the output columns a parent with `supplies == i` fed. None
    # means the parent's contribution is not narrowed to particular columns —
    # true of a filter or union row, which passed through whole, and of any
    # producer that does not attribute. Entry 0 is always None.
    supplies: list[tuple[str, ...] | None] = field(
        default_factory=lambda: [None]
    )

    def __post_init__(self) -> None:
        for entry in self.parents:
            if not isinstance(entry, list):
                raise ValueError("row lineage needs a list of parents per output row")
        if not self.supplies or self.supplies[WHOLE_ROW] is not None:
            raise ValueError("supplies entry 0 must be None — the whole-row default")

    def __len__(self) -> int:
        return len(self.parents)

    def columns_supplied_by(self, parent: RowParent) -> tuple[str, ...] | None:
        """The output columns `parent` fed, or None where it supplied the whole row."""
        return self.supplies[parent.supplies] if parent.supplies < len(self.supplies) else None

    def shifted(self, offset: int) -> "RowLineage":
        """Ordinals counted from a sliced input frame's first row, moved onto the upstream's own."""
        # ONE offset covers every parent of every row: the runtime cuts the same
        # window out of each of a stage's inputs, so a join's two sides shift together.
        if offset == 0:
            return self
        return RowLineage([
            [RowParent(p.stage_id, p.row_ordinal + offset, p.kind, p.supplies) for p in entry]
            for entry in self.parents
        ], list(self.supplies))

    def to_frame(self) -> pd.DataFrame:
        """One row per output row; four parallel per-parent lists plus the supplies table."""
        table = json.dumps([None if s is None else list(s) for s in self.supplies])
        # Repeated on every row because the table is per STAGE: parquet
        # dictionary-encodes a constant column to nothing, and it keeps the
        # sidecar a plain frame any reader can open.
        return pd.DataFrame({
            TRACE_SOURCE_STAGE_KEY: pd.Series(
                [[p.stage_id for p in entry] for entry in self.parents], dtype=object),
            TRACE_SOURCE_ROW_KEY: pd.Series(
                [[p.row_ordinal for p in entry] for entry in self.parents], dtype=object),
            TRACE_EDGE_KIND_KEY: pd.Series(
                [[str(p.kind) for p in entry] for entry in self.parents], dtype=object),
            TRACE_SUPPLIES_KEY: pd.Series(
                [[int(p.supplies) for p in entry] for entry in self.parents], dtype=object),
            TRACE_SUPPLIES_TABLE_KEY: pd.Series(
                [table] * len(self.parents), dtype=object),
        })

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "RowLineage":
        """Read a sidecar frame back, including one written before lineage went multi-parent."""
        # A pre-multi-parent sidecar held SCALARS and no kind column, and a
        # pre-supplies one named no columns — each reads as one whole-row direct
        # parent, so old runs stay traceable without a migration.
        has_kind = TRACE_EDGE_KIND_KEY in df.columns
        has_supplies = TRACE_SUPPLIES_KEY in df.columns
        parents: list[list[RowParent]] = []
        for i in range(len(df)):
            stages = _as_list(df[TRACE_SOURCE_STAGE_KEY].iloc[i])
            rows = _as_list(df[TRACE_SOURCE_ROW_KEY].iloc[i])
            kinds = _as_list(df[TRACE_EDGE_KIND_KEY].iloc[i]) if has_kind else []
            supplies = _as_list(df[TRACE_SUPPLIES_KEY].iloc[i]) if has_supplies else []
            entry = [
                RowParent(
                    stage_id=str(stages[k]),
                    row_ordinal=int(rows[k]),
                    kind=str(kinds[k]) if k < len(kinds) else EdgeKind.direct.value,
                    supplies=int(supplies[k]) if k < len(supplies) else WHOLE_ROW,
                )
                for k in range(min(len(stages), len(rows)))
            ]
            parents.append(entry)
        return cls(parents, _read_supplies_table(df))


def _read_supplies_table(df: pd.DataFrame) -> list[tuple[str, ...] | None]:
    """The stage's supplies table off any row of the sidecar; whole-row-only when absent."""
    if TRACE_SUPPLIES_TABLE_KEY not in df.columns or len(df) == 0:
        return [None]
    raw = json.loads(str(df[TRACE_SUPPLIES_TABLE_KEY].iloc[0]))
    return [None if entry is None else tuple(str(c) for c in entry) for entry in raw]


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
    """For a union: read off the input row counts alone, so the stage is not consulted."""
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


def merged_inputs_lineage(
    inputs: Sequence[tuple[str, Iterable[Any]]],
) -> RowLineage:
    """At most one row from each of N inputs per output row; FIRST is the one the walk follows."""
    # Ordinals arrive as read back off the merged frame, so an input that did not
    # match this row is NaN/None and is then simply absent from its parents —
    # that absence IS the recorded non-match. Order carries the preference: the
    # earliest input that did match becomes the spine, so a row where the
    # preferred one is absent still gets walked, on the data rather than a default.
    parents: list[list[RowParent]] = []
    for ordinals in zip(*(rows for _stage_id, rows in inputs)):
        parents.append([
            RowParent(stage_id, int(ordinal))
            for (stage_id, _rows), ordinal in zip(inputs, ordinals)
            if not _is_missing(ordinal)
        ])
    return RowLineage(parents)


def _is_missing(value: Any) -> bool:
    """True for an ordinal a merge left unmatched (NaN, None and pd.NA alike)."""
    return value is None or bool(pd.isna(value))


def grouped_contributions_lineage(
    source_stage_id: str, contributors: list[dict[int, tuple[str, ...]]]
) -> RowLineage:
    """For an aggregate: entry i names every input row that fed output row i, and what it fed."""
    table: list[tuple[str, ...] | None] = [None]
    # A row appears ONCE however many columns it fed, so this stays O(input
    # rows) rather than O(rows x aggregations); the column tuples intern because
    # there are only as many distinct ones as there are `where` filters.
    index_of: dict[tuple[str, ...], int] = {}
    parents: list[list[RowParent]] = []
    for row_contributors in contributors:
        entry: list[RowParent] = []
        for ordinal, columns in sorted(row_contributors.items()):
            if columns not in index_of:
                index_of[columns] = len(table)
                table.append(columns)
            entry.append(RowParent(
                source_stage_id, int(ordinal),
                EdgeKind.contribution.value, index_of[columns],
            ))
        parents.append(entry)
    return RowLineage(parents, table)


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    """Where a stage's sidecar lives, alongside its output parquet in the run dir."""
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"
