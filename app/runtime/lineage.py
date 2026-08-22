"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME wherever the stage's shape
settles it, and reported by the stage itself for python_frame_function, which
reshapes arbitrarily — see `LineageRecorder`. A row may have several parents, so
the sidecar is list-valued; it is a field on `StageOutput`, never a frame column."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from app.core.errors import (
    LineageIncomplete,
    RowOutOfRange,
    StageNotInRun,
)

import numpy as np
import pandas as pd
import pyarrow as pa

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
    # The file a source stage read this row from; `row_ordinal` counts within it.
    source_file: str | None = None
    source_file_sha: str | None = None


@dataclass(frozen=True)
class RowLineage:
    """Entry i is the list of parents of output row i, spine first, in output order."""

    parents: list[list[RowParent]] = field(default_factory=list)

    def __post_init__(self) -> None:
        for entry in self.parents:
            if not isinstance(entry, list):
                raise ValueError("row lineage needs a list of parents per output row")

    def __len__(self) -> int:
        return len(self.parents)

    def shifted(self, offset: int) -> "RowLineage":
        # One offset covers every parent: the runtime cuts the same window out of each input.
        if offset == 0:
            return self
        return RowLineage([
            [RowParent(p.stage_id, p.row_ordinal + offset, p.kind, p.columns,
                       p.source_file, p.source_file_sha)
             for p in entry]
            for entry in self.parents
        ])

    def sliced(self, start: int, length: int | None) -> "RowLineage":
        """For a stage whose rows come from outside the run: a window CUTS them, never moves them."""
        end = len(self.parents) if length is None else start + length
        return RowLineage(self.parents[start:end])

    def to_table(self) -> pa.Table:
        """Raises rather than writing a sidecar arrow would type differently from its siblings."""
        return pa.table({
            TRACE_SOURCE_STAGE_KEY: [[p.stage_id for p in entry] for entry in self.parents],
            TRACE_SOURCE_ROW_KEY: [[p.row_ordinal for p in entry] for entry in self.parents],
            TRACE_EDGE_KIND_KEY: [[str(p.kind) for p in entry] for entry in self.parents],
            TRACE_SOURCE_COLUMNS_KEY: [
                [list(p.columns or ()) for p in entry] for entry in self.parents],
            TRACE_SOURCE_FILE_KEY: [
                [p.source_file or "" for p in entry] for entry in self.parents],
            TRACE_SOURCE_SHA_KEY: [
                [p.source_file_sha or "" for p in entry] for entry in self.parents],
        }, schema=LINEAGE_SCHEMA)

    @classmethod
    def from_table(cls, table: pa.Table) -> "RowLineage":
        """The absent columns are pre-multi-parent sidecars; old runs stay readable unmigrated."""
        stage_cells = _column_cells(table, TRACE_SOURCE_STAGE_KEY)  # once, not per row:
        # reading a column inside the loop re-boxes the whole column every time,
        # which on a 45k-row sidecar costs seconds.
        row_cells = _column_cells(table, TRACE_SOURCE_ROW_KEY)
        kind_cells = _column_cells(table, TRACE_EDGE_KIND_KEY)
        column_cells = _column_cells(table, TRACE_SOURCE_COLUMNS_KEY)
        file_cells = _column_cells(table, TRACE_SOURCE_FILE_KEY)
        sha_cells = _column_cells(table, TRACE_SOURCE_SHA_KEY)
        return cls([
            _read_parents(stage_cells[i], row_cells[i], kind_cells[i], column_cells[i],
                          file_cells[i], sha_cells[i])
            for i in range(table.num_rows)
        ])


def _read_parents(stages: Any, rows: Any, kinds: Any, columns: Any,
                  files: Any = None, shas: Any = None) -> list[RowParent]:
    stage_ids, row_ordinals = _as_list(stages), _as_list(rows)
    kind_names, column_names = _as_list(kinds), _as_list(columns)
    filenames, digests = _as_list(files), _as_list(shas)
    return [
        RowParent(
            stage_id=str(stage_ids[k]),
            row_ordinal=int(row_ordinals[k]),
            kind=str(kind_names[k]) if k < len(kind_names) else EdgeKind.direct.value,
            columns=_columns_or_none(column_names[k]) if k < len(column_names) else None,
            source_file=str(filenames[k]) or None if k < len(filenames) else None,
            source_file_sha=str(digests[k]) or None if k < len(digests) else None,
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


def lineage_sidecar_path(run_dir: Path, stage_id: str) -> Path:
    return Path(run_dir) / "outputs" / f"{stage_id}.lineage.parquet"


# The keyword a frame function declares to be handed the recorder. Keyword-ONLY:
# the positional slots are the input frames, so a positional `lineage` could not be
# told apart from a third input.
LINEAGE_KWARG = "lineage"


@dataclass(frozen=True)
class LineageRecorder:
    """A frame function's own account of which input rows became which output row."""

    tables: Mapping[str, pa.Table]  # this stage's inputs: the only rows it may claim
    # `frozen` stops these being rebound, not written, which is what lets the recorder
    # handed to authored code come back carrying what that code said.
    spoken_for: dict[int, list[RowParent]] = field(default_factory=dict)

    def built_from(self, row_ordinal: int, stage_id: str, source_row: int,
                   columns: Iterable[str] | None = None) -> None:
        """Output row `row_ordinal` is this input row, reshaped."""
        self._record(row_ordinal, stage_id, source_row, EdgeKind.direct.value, columns)

    def contributed_by(self, row_ordinal: int, stage_id: str, source_row: int,
                       columns: Iterable[str] | None = None) -> None:
        """This input row fed the output row without being the row it was built from."""
        self._record(row_ordinal, stage_id, source_row, EdgeKind.contribution.value, columns)

    def originates(self, row_ordinal: int) -> None:
        """No input row became this one; `resolve` refuses silence but accepts this."""
        self.spoken_for.setdefault(int(row_ordinal), [])

    def resolve(self, row_count: int) -> RowLineage:
        """Refuses a partial account — an unspoken-for row would trace as parentless."""
        beyond = sorted(r for r in self.spoken_for if not 0 <= r < row_count)
        if beyond:
            raise RowOutOfRange(
                f"lineage was recorded for output row(s) {beyond}, but the stage "
                f"returned {row_count} row(s)"
            )
        unspoken = [r for r in range(row_count) if r not in self.spoken_for]
        if unspoken:
            raise LineageIncomplete(
                f"lineage was recorded for {len(self.spoken_for)} of {row_count} output "
                f"row(s); {len(unspoken)} were not spoken for, first at {unspoken[0]}. "
                f"Every row needs a parent or an explicit `originates(...)` — a row left "
                f"out would trace as having come from nowhere."
            )
        return RowLineage([self.spoken_for[r] for r in range(row_count)])

    def _record(self, row_ordinal: int, stage_id: str, source_row: int,
                kind: str, columns: Iterable[str] | None) -> None:
        table = self.tables.get(stage_id)
        if table is None:
            raise StageNotInRun(
                f"this stage was not given '{stage_id}', so it cannot have read its "
                f"rows — it holds {sorted(self.tables)}"
            )
        if not 0 <= source_row < table.num_rows:
            raise RowOutOfRange(
                f"row {source_row} out of range for input '{stage_id}' "
                f"({table.num_rows} rows)"
            )
        named = tuple(str(c) for c in columns) if columns else None
        self.spoken_for.setdefault(int(row_ordinal), []).append(
            RowParent(stage_id, int(source_row), kind, named)
        )
