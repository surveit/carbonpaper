"""Per-row provenance for a stage whose output isn't row-preserving BY POSITION
(filter_rows, union, join), worked out by the RUNTIME, never reported by the
authored stage. It rides the output frame's `.attrs` rather than columns on it,
so no runtime machinery can reach a stage's real output. A row may have several
parents, so the sidecar is list-valued — see `RowLineage`."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from app.models.workflow_stage import WorkflowStage

TRACE_SOURCE_STAGE_KEY = "_trace_source_stage"
TRACE_SOURCE_ROW_KEY = "_trace_source_row"
TRACE_EDGE_KIND_KEY = "_trace_edge_kind"
TRACE_SOURCE_COLUMNS_KEY = "_trace_source_columns"

# The `.attrs` channel the row driver hands lineage out on. The executor reads
# it BEFORE any row slicing and pops it before persisting — `.attrs` does not
# survive parquet. The row-grain cache sits below this: a row replayed from
# cache fills its slot exactly as a computed one does, so the two never
# interact.
LINEAGE_ATTR = "row_lineage"


class EdgeKind(str, Enum):
    # An enrich's subject row, and the reference row merged into it.
    direct = "direct"
    # Every filing in the quarter an aggregate totalled into one row.
    contribution = "contribution"
    # A python_frame_function pivoted the frame: this input fed the output, but
    # which of its rows fed THIS row was not recoverable.
    unknown = "unknown"


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
            [RowParent(p.stage_id, p.row_ordinal + offset, p.kind, p.columns) for p in entry]
            for entry in self.parents
        ])

    def to_frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            TRACE_SOURCE_STAGE_KEY: pd.Series(
                [[p.stage_id for p in entry] for entry in self.parents], dtype=object),
            TRACE_SOURCE_ROW_KEY: pd.Series(
                [[p.row_ordinal for p in entry] for entry in self.parents], dtype=object),
            TRACE_EDGE_KIND_KEY: pd.Series(
                [[str(p.kind) for p in entry] for entry in self.parents], dtype=object),
            TRACE_SOURCE_COLUMNS_KEY: pd.Series(
                [[list(p.columns or ()) for p in entry] for entry in self.parents],
                dtype=object),
        })

    @classmethod
    def from_frame(cls, df: pd.DataFrame) -> "RowLineage":
        """The absent columns are pre-multi-parent sidecars; old runs stay readable unmigrated."""
        has_kind = TRACE_EDGE_KIND_KEY in df.columns
        has_columns = TRACE_SOURCE_COLUMNS_KEY in df.columns
        parents: list[list[RowParent]] = []
        for i in range(len(df)):
            stages = _as_list(df[TRACE_SOURCE_STAGE_KEY].iloc[i])
            rows = _as_list(df[TRACE_SOURCE_ROW_KEY].iloc[i])
            kinds = _as_list(df[TRACE_EDGE_KIND_KEY].iloc[i]) if has_kind else []
            columns = _as_list(df[TRACE_SOURCE_COLUMNS_KEY].iloc[i]) if has_columns else []
            entry = [
                RowParent(
                    stage_id=str(stages[k]),
                    row_ordinal=int(rows[k]),
                    kind=str(kinds[k]) if k < len(kinds) else EdgeKind.direct.value,
                    columns=_columns_or_none(columns[k]) if k < len(columns) else None,
                )
                for k in range(min(len(stages), len(rows)))
            ]
            parents.append(entry)
        return cls(parents)


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


def attach_row_lineage(df: pd.DataFrame, lineage: RowLineage) -> pd.DataFrame:
    """`.attrs` does not survive a rebuild — call this on the frame the handler RETURNS."""
    df.attrs[LINEAGE_ATTR] = lineage
    return df


def read_row_lineage(df: pd.DataFrame | None) -> RowLineage | None:
    if df is None:
        return None
    attached = df.attrs.get(LINEAGE_ATTR)
    return attached if isinstance(attached, RowLineage) else None


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
