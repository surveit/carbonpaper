"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME, never reported by the
authored stage. It is a field on `StageOutput`, never a column on the frame, so
no runtime machinery can reach a stage's real output. A row may have several
parents, so the sidecar is list-valued — see `RowLineage`."""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"
TRACE_EDGE_KIND_KEY = "_trace_edge_kind"
TRACE_SOURCE_COLUMNS_KEY = "_trace_source_columns"
TRACE_RUN_LENGTH_KEY = "_trace_run_length"

# Pinned: left to infer, an empty sidecar types every column `null`.
LINEAGE_SCHEMA = pa.schema([
    (TRACE_SOURCE_STAGE_KEY, pa.list_(pa.string())),
    (TRACE_SOURCE_ROW_KEY, pa.list_(pa.int64())),
    (TRACE_EDGE_KIND_KEY, pa.list_(pa.string())),
    (TRACE_SOURCE_COLUMNS_KEY, pa.list_(pa.list_(pa.string()))),
    (TRACE_RUN_LENGTH_KEY, pa.int64()),
])


class EdgeKind(str, Enum):
    # An enrich's subject row, and the reference row merged into it.
    direct = "direct"
    # Every filing in the quarter an aggregate totalled into one row.
    contribution = "contribution"


@dataclass(frozen=True)
class RowParent:
    stage_id: str
    row_ordinal: int
    kind: str = EdgeKind.direct.value
    # The output columns this parent fed. None means its contribution is not
    # narrowed to particular columns — true of a filter or union row, which
    # passed through whole, and of any producer that does not attribute.
    columns: tuple[str, ...] | None = None


@dataclass(frozen=True)
class LineageRun:
    """Covers `length` output rows; every parent's ordinal advances by one per row."""

    length: int
    parents: list[RowParent]

    def __post_init__(self) -> None:
        # Two runs would share a start, and the row search would pick the wrong one.
        if self.length < 1:
            raise ValueError(f"a lineage run covers at least one row, got {self.length}")

    def parents_at(self, offset: int) -> list[RowParent]:
        return _advanced(self.parents, offset)


@dataclass(frozen=True)
class RowLineage:
    """Output rows in order, grouped into runs; entry i of `parents` is row i's parents."""

    runs: list[LineageRun] = field(default_factory=list)
    # Each run's first output row, so parents_of binary-searches instead of walking.
    _starts: list[int] = field(init=False, repr=False, compare=False, default_factory=list)
    _row_count: int = field(init=False, repr=False, compare=False, default=0)

    def __post_init__(self) -> None:
        starts: list[int] = []
        total = 0
        for run in self.runs:
            if not isinstance(run, LineageRun):
                raise ValueError(
                    "row lineage takes LineageRun entries — for one run per row, "
                    "build it with explicit_lineage()")
            starts.append(total)
            total += run.length
        object.__setattr__(self, "_starts", starts)
        object.__setattr__(self, "_row_count", total)

    def __len__(self) -> int:
        return self._row_count

    @property
    def parents(self) -> list[list[RowParent]]:
        """Materializes every row. Use `parents_of` for one row — a run may cover millions."""
        return list(self.iter_parents())

    def parents_of(self, row_ordinal: int) -> list[RowParent]:
        if not 0 <= row_ordinal < self._row_count:
            raise IndexError(
                f"row {row_ordinal} is outside this lineage's {self._row_count} rows")
        index = bisect_right(self._starts, row_ordinal) - 1
        return self.runs[index].parents_at(row_ordinal - self._starts[index])

    def iter_parents(self) -> Iterator[list[RowParent]]:
        for run in self.runs:
            for offset in range(run.length):
                yield run.parents_at(offset)

    def shifted(self, offset: int) -> "RowLineage":
        # One offset covers every parent: the runtime cuts the same window out of each input.
        if offset == 0:
            return self
        return RowLineage(
            [LineageRun(run.length, _advanced(run.parents, offset)) for run in self.runs])

    def to_table(self) -> pa.Table:
        """Raises rather than writing a sidecar arrow would type differently from its siblings."""
        return pa.table({
            TRACE_SOURCE_STAGE_KEY: [[p.stage_id for p in r.parents] for r in self.runs],
            TRACE_SOURCE_ROW_KEY: [[p.row_ordinal for p in r.parents] for r in self.runs],
            TRACE_EDGE_KIND_KEY: [[str(p.kind) for p in r.parents] for r in self.runs],
            TRACE_SOURCE_COLUMNS_KEY: [
                [list(p.columns or ()) for p in r.parents] for r in self.runs],
            TRACE_RUN_LENGTH_KEY: [r.length for r in self.runs],
        }, schema=LINEAGE_SCHEMA)

    @classmethod
    def from_table(cls, table: pa.Table) -> "RowLineage":
        """The absent columns are older sidecars; old runs stay readable unmigrated."""
        stage_cells = _column_cells(table, TRACE_SOURCE_STAGE_KEY)  # once, not per row:
        # reading a column inside the loop re-boxes the whole column every time,
        # which on a 45k-row sidecar costs seconds.
        row_cells = _column_cells(table, TRACE_SOURCE_ROW_KEY)
        kind_cells = _column_cells(table, TRACE_EDGE_KIND_KEY)
        column_cells = _column_cells(table, TRACE_SOURCE_COLUMNS_KEY)
        lengths = _run_lengths(table)
        return cls([
            LineageRun(
                lengths[i],
                _read_parents(stage_cells[i], row_cells[i], kind_cells[i], column_cells[i]))
            for i in range(table.num_rows)
        ])


def explicit_lineage(parents: Sequence[Sequence[RowParent]]) -> RowLineage:
    """One run per output row — for a producer whose rows do not advance in step."""
    return RowLineage([LineageRun(1, list(entry)) for entry in parents])


def _advanced(parents: Sequence[RowParent], by: int) -> list[RowParent]:
    if by == 0:
        return list(parents)
    return [RowParent(p.stage_id, p.row_ordinal + by, p.kind, p.columns) for p in parents]


def _run_lengths(table: pa.Table) -> list[int]:
    """A sidecar written before runs existed carries no lengths and is one row per entry."""
    if TRACE_RUN_LENGTH_KEY not in table.column_names:
        return [1] * table.num_rows
    lengths = table.column(TRACE_RUN_LENGTH_KEY).to_pylist()
    if any(length is None for length in lengths):
        # Defaulting to 1 would silently renumber every row after the null.
        raise ValueError(
            f"sidecar column {TRACE_RUN_LENGTH_KEY} holds a null; it was written wrong")
    return [int(length) for length in lengths]


def _read_parents(stages: Any, rows: Any, kinds: Any, columns: Any) -> list[RowParent]:
    stage_ids, row_ordinals = _as_list(stages), _as_list(rows)
    kind_names, column_names = _as_list(kinds), _as_list(columns)
    return [
        RowParent(
            stage_id=str(stage_ids[k]),
            row_ordinal=int(row_ordinals[k]),
            kind=str(kind_names[k]) if k < len(kind_names) else EdgeKind.direct.value,
            columns=_columns_or_none(column_names[k]) if k < len(column_names) else None,
        )
        for k in range(min(len(stage_ids), len(row_ordinals)))
    ]


def _column_cells(table: pa.Table, name: str) -> list[Any]:
    """[] for every row when the column is absent, so a pre-multi-parent sidecar still reads."""
    if name not in table.column_names:
        return [[]] * table.num_rows
    return table.column(name).to_pylist()


def _columns_or_none(cell: Any) -> tuple[str, ...] | None:
    names = _as_list(cell)
    return tuple(str(c) for c in names) if names else None


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return []
    return [value]


def single_parent_lineage(
    source_stage_id: str, source_rows: Iterable[int]
) -> RowLineage:
    return explicit_lineage([
        [RowParent(source_stage_id, int(r))] for r in source_rows
    ])


def contiguous_parent_lineage(
    source_stage_id: str, first_source_row: int, rows: int
) -> RowLineage:
    """One run: output row k came from `source_stage_id` row `first_source_row + k`."""
    if rows < 1:
        return RowLineage([])
    return RowLineage([LineageRun(rows, [RowParent(source_stage_id, first_source_row)])])


def kept_rows_lineage(source_stage_id: str, kept_indices: list[int]) -> RowLineage:
    return single_parent_lineage(source_stage_id, kept_indices)


def concatenated_inputs_lineage(
    workflow_stage: "WorkflowStage", inputs: dict[str, pd.DataFrame],
    first_row_ordinal: int = 0,
) -> RowLineage:
    # A concatenation says a block of output rows tracks a block of one input's.
    return RowLineage([
        LineageRun(len(inputs[ref.id]), [RowParent(ref.id, first_row_ordinal)])
        for ref in workflow_stage.inputs
        if len(inputs[ref.id]) > 0
    ])


def merged_inputs_lineage(
    inputs: Sequence[tuple[str, Iterable[Any]]],
) -> RowLineage:
    """`inputs` order is the spine preference; an unmatched input is absent, recording a non-match."""
    parents: list[list[RowParent]] = []
    for ordinals in zip(*(rows for _stage_id, rows in inputs)):
        parents.append([
            RowParent(stage_id, int(ordinal))
            for (stage_id, _rows), ordinal in zip(inputs, ordinals)
            if not _is_missing(ordinal)
        ])
    return explicit_lineage(parents)


def _is_missing(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def grouped_contributions_lineage(
    source_stage_id: str, contributors: list[dict[int, tuple[str, ...]]]
) -> RowLineage:
    return explicit_lineage([
        # A row appears ONCE carrying every column it fed, which is what keeps
        # this O(input rows) rather than O(rows x aggregations).
        [
            RowParent(source_stage_id, int(ordinal), EdgeKind.contribution.value, columns)
            for ordinal, columns in sorted(row_contributors.items())
        ]
        for row_contributors in contributors
    ])


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"
