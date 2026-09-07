"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME, never reported by the
authored stage. It is a field on `StageOutput`, never a column on the frame, so
no runtime machinery can reach a stage's real output. A row may have several
parents, so the sidecar is list-valued — see `RowLineage`."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Iterable,
    Iterator,
    NamedTuple,
    Sequence,
    TypeVar,
    overload,
)

import numpy as np
import pandas as pd
import pyarrow as pa

from app.core.frames import read_native_cell, read_native_column
from app.models.run_manifest import ReadFile

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"
TRACE_EDGE_KIND_KEY = "_trace_edge_kind"
TRACE_SOURCE_COLUMNS_KEY = "_trace_source_columns"
TRACE_SOURCE_FILE_KEY = "_trace_source_file"
TRACE_SOURCE_SHA_KEY = "_trace_source_sha"

# Pinned: left to infer, an empty sidecar types every column `null`.
LINEAGE_SCHEMA = pa.schema([
    (TRACE_SOURCE_STAGE_KEY, pa.list_(pa.string())),
    (TRACE_SOURCE_ROW_KEY, pa.list_(pa.int64())),
    (TRACE_EDGE_KIND_KEY, pa.list_(pa.string())),
    (TRACE_SOURCE_COLUMNS_KEY, pa.list_(pa.list_(pa.string()))),
    (TRACE_SOURCE_FILE_KEY, pa.list_(pa.string())),
    (TRACE_SOURCE_SHA_KEY, pa.list_(pa.string())),
])

LINEAGE_KEYS = (TRACE_SOURCE_STAGE_KEY, TRACE_SOURCE_ROW_KEY, TRACE_EDGE_KIND_KEY,
                TRACE_SOURCE_COLUMNS_KEY, TRACE_SOURCE_FILE_KEY, TRACE_SOURCE_SHA_KEY)


class EdgeKind(str, Enum):
    # An enrich's subject row, and the reference row merged into it.
    direct = "direct"
    # Every filing in the quarter an aggregate totalled into one row.
    contribution = "contribution"


@dataclass(frozen=True)
class RowParent:
    """One step upstream: `row_ordinal` indexes `stage_id`'s own output frame."""

    stage_id: str
    row_ordinal: int
    kind: str = EdgeKind.direct.value
    # The output columns this parent fed. None means its contribution is not
    # narrowed to particular columns — true of a filter or union row, which
    # passed through whole, and of any producer that does not attribute.
    columns: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RowSource:
    """Where a load read a row off disk: `row_ordinal` counts within `file`, not in the frame."""

    file: str
    file_sha: str | None
    row_ordinal: int


@dataclass(frozen=True)
class RowLineage:
    """`parents[i]` holds output row i's parents, spine first; `sources[i]` the file it was read from."""

    parents: Sequence[list[RowParent]] = field(default_factory=list)
    # One slot per row, None everywhere but a load. Left out, it fills itself with Nones.
    sources: Sequence[RowSource | None] = ()

    def __post_init__(self) -> None:
        if not self.sources and self.parents:
            object.__setattr__(self, "sources", [None] * len(self.parents))
        if len(self.sources) != len(self.parents):
            raise ValueError("row lineage needs one source slot per output row")
        # Walking a decoded sidecar here would undo what `from_table` avoids.
        if isinstance(self.parents, _SidecarHalf):
            return
        for entry in self.parents:
            if not isinstance(entry, list):
                raise ValueError("row lineage needs a list of parents per output row")

    def __len__(self) -> int:
        return len(self.parents)

    def source(self, row_ordinal: int) -> RowSource | None:
        return (self.sources[row_ordinal]
                if 0 <= row_ordinal < len(self.sources) else None)

    def shifted(self, offset: int) -> "RowLineage":
        # One offset covers every parent: the runtime cuts the same window out of each input.
        if offset == 0:
            return self
        return RowLineage(
            [[RowParent(p.stage_id, p.row_ordinal + offset, p.kind, p.columns) for p in entry]
             for entry in self.parents],
            list(self.sources),  # a source counts within its file, which no window moves
        )

    def sliced(self, start: int, length: int | None) -> "RowLineage":
        """For a stage whose rows come from outside the run: a window CUTS them, never moves them."""
        end = len(self.parents) if length is None else start + length
        return RowLineage(self.parents[start:end], self.sources[start:end])

    def to_table(self) -> pa.Table:
        """Raises rather than writing a sidecar arrow would type differently from its siblings."""
        cells: dict[str, list[list[Any]]] = {key: [] for key in LINEAGE_KEYS}
        for row in range(len(self)):
            encoded = _encode_row(self.parents[row], self.source(row))
            for key, cell in zip(LINEAGE_KEYS, encoded):
                cells[key].append(cell)
        return pa.table(cells, schema=LINEAGE_SCHEMA)

    @classmethod
    def from_table(cls, table: pa.Table) -> "RowLineage":
        """The absent columns are pre-multi-parent sidecars; old runs stay readable unmigrated."""
        rows = _SidecarRows(table)
        return cls(_SidecarHalf(rows, lambda row: row.parents),
                   _SidecarHalf(rows, lambda row: row.source))


def single_parent_lineage(
    source_stage_id: str, source_rows: Iterable[int]
) -> RowLineage:
    return RowLineage([
        [RowParent(source_stage_id, int(r))] for r in source_rows
    ])


def kept_rows_lineage(source_stage_id: str, kept_indices: list[int]) -> RowLineage:
    return single_parent_lineage(source_stage_id, kept_indices)


def concatenated_inputs_lineage(
    workflow_stage: "WorkflowStage", inputs: dict[str, pd.DataFrame],
    first_row_ordinal: int = 0,
) -> RowLineage:
    parents: list[list[RowParent]] = []
    for ref in workflow_stage.inputs:
        rows = len(inputs[ref.id])
        parents.extend(
            [RowParent(ref.id, r)]
            for r in range(first_row_ordinal, first_row_ordinal + rows)
        )
    return RowLineage(parents)


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
    return RowLineage(parents)


def _is_missing(value: Any) -> bool:
    return value is None or bool(pd.isna(value))


def grouped_contributions_lineage(
    source_stage_id: str, contributors: list[dict[int, tuple[str, ...]]]
) -> RowLineage:
    return RowLineage([
        # A row appears ONCE carrying every column it fed, which is what keeps
        # this O(input rows) rather than O(rows x aggregations).
        [
            RowParent(source_stage_id, int(ordinal), EdgeKind.contribution.value, columns)
            for ordinal, columns in sorted(row_contributors.items())
        ]
        for row_contributors in contributors
    ])


def read_files_lineage(
    files: Sequence[ReadFile], rows_per_file: Sequence[int]
) -> RowLineage:
    """A load has no parent stage: what a reader asks here is which FILE, not which step."""
    sources: list[RowSource | None] = [
        RowSource(one.path, one.sha256, row)
        for one, rows in zip(files, rows_per_file)
        for row in range(rows)
    ]
    return RowLineage([[] for _ in sources], sources)


# ─── the sidecar's own encoding ───────────────────
# docs/branch-analysis.md

def _encode_row(parents: list[RowParent], source: RowSource | None) -> tuple[list[Any], ...]:
    return (
        [p.stage_id for p in parents] + ([""] if source else []),
        [p.row_ordinal for p in parents] + ([source.row_ordinal] if source else []),
        [str(p.kind) for p in parents] + ([""] if source else []),
        [list(p.columns or ()) for p in parents] + ([[]] if source else []),
        ["" for _ in parents] + ([source.file] if source else []),
        ["" for _ in parents] + ([source.file_sha or ""] if source else []),
    )


class _DecodedRow(NamedTuple):
    parents: list[RowParent]
    source: RowSource | None


def _decode_row(stages: Any, rows: Any, kinds: Any, columns: Any,
                files: Any, shas: Any) -> _DecodedRow:
    stage_ids, row_ordinals = _as_list(stages), _as_list(rows)
    kind_names, column_names = _as_list(kinds), _as_list(columns)
    filenames, digests = _as_list(files), _as_list(shas)
    parents: list[RowParent] = []
    source: RowSource | None = None
    for k in range(min(len(stage_ids), len(row_ordinals))):
        read_from = str(filenames[k]) if k < len(filenames) else ""
        if read_from:
            source = RowSource(
                read_from,
                (str(digests[k]) or None) if k < len(digests) else None,
                int(row_ordinals[k]),
            )
            continue
        parents.append(RowParent(
            stage_id=str(stage_ids[k]),
            row_ordinal=int(row_ordinals[k]),
            kind=str(kind_names[k]) if k < len(kind_names) else EdgeKind.direct.value,
            columns=_columns_or_none(column_names[k]) if k < len(column_names) else None,
        ))
    return _DecodedRow(parents, source)


class _SidecarRows:
    """A trace reads one row per stage; branch analysis reads every row of one."""

    def __init__(self, table: pa.Table) -> None:
        self._table = table
        self._one_at_a_time: dict[int, _DecodedRow] = {}
        self._every_row: list[_DecodedRow] | None = None

    def __len__(self) -> int:
        return int(self._table.num_rows)

    def read_row(self, index: int) -> _DecodedRow:
        if self._every_row is not None:
            return self._every_row[index]
        if index not in self._one_at_a_time:
            self._one_at_a_time[index] = _decode_row(*self._cells_of_row(index))
        return self._one_at_a_time[index]

    def read_every_row(self) -> list[_DecodedRow]:
        # Boxing each column once beats indexing it per row on a whole-sidecar read.
        if self._every_row is None:
            held = [_column_cells(self._table, key) for key in LINEAGE_KEYS]
            self._every_row = [_decode_row(*(column[i] for column in held))
                               for i in range(len(self))]
        return self._every_row

    def _cells_of_row(self, index: int) -> list[Any]:
        return [read_native_cell(self._table, key, index)
                if key in self._table.column_names else None
                for key in LINEAGE_KEYS]


_Half = TypeVar("_Half")


class _SidecarHalf(Sequence[_Half]):
    """The parents, or the sources, of a sidecar whose rows are decoded on demand."""

    def __init__(self, rows: _SidecarRows, pick: Callable[[_DecodedRow], _Half]) -> None:
        self._rows = rows
        self._pick = pick

    def __len__(self) -> int:
        return len(self._rows)

    @overload
    def __getitem__(self, index: int) -> _Half: ...
    @overload
    def __getitem__(self, index: slice) -> list[_Half]: ...

    def __getitem__(self, index: int | slice) -> Any:
        if isinstance(index, slice):
            return [self._pick(row) for row in self._rows.read_every_row()[index]]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return self._pick(self._rows.read_row(index))

    def __iter__(self) -> Iterator[_Half]:
        return iter([self._pick(row) for row in self._rows.read_every_row()])

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Sequence):
            return list(self) == list(other)
        return NotImplemented

    __hash__ = None  # type: ignore[assignment]


def _column_cells(table: pa.Table, name: str) -> list[Any]:
    """[] for every row when the column is absent, so a pre-multi-parent sidecar still reads."""
    if name not in table.column_names:
        return [[]] * table.num_rows
    return read_native_column(table, name)


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
