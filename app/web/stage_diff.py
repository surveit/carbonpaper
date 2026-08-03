"""Stage-aware diff for the run stage panel's Outputs pane: a 1:1 stage's
output read against its input frame cell by cell, and a filter_rows stage's
dropped rows read off its runtime-recorded lineage sidecar. Anything
unverifiable — missing frame, row-count mismatch, absent sidecar — yields
None and the pane falls back to the plain output view, never a guessed alignment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Optional, Union

import pandas as pd

from app.models import Stage, StageType
from app.runtime.lineage import (
    TRACE_SOURCE_ROW_KEY,
    TRACE_SOURCE_STAGE_KEY,
    lineage_sidecar_path,
)
from app.web.loading import read_table

# The 1:1-by-position stage types the aligned diff covers: their runtime
# contract maps output row i to input row i, so a positional comparison states
# facts. Deliberately NOT is_grain_and_order_preserving(), which also admits
# input_data (no input to diff against) and human_review_queue (out of scope).
ROW_ALIGNED_TYPES: frozenset[StageType] = frozenset({
    StageType.python_row_function,
    StageType.llm_transform,
})

# Row budgets for the rendered tables. Change COUNTS always cover the whole
# frame; only the rows drawn are capped (aligned: same first-5 window the plain
# output preview shows; dropped: the dropped subset is the point, so more room).
ALIGNED_ROWS_SHOWN = 5
DROPPED_ROWS_SHOWN = 50

ROW_ALIGNED_KIND = "row_aligned"
DROPPED_ROWS_KIND = "dropped_rows"


@dataclass(frozen=True)
class DiffCell:
    """One output cell: its rendered text, and the input value it replaced (if any)."""

    text: str
    was: Optional[str]
    changed: bool
    added: bool


@dataclass(frozen=True)
class DiffColumn:
    """One output column: whether the stage added it, and its whole-frame changed-cell count."""

    name: str
    added: bool
    changed_cells: int


@dataclass(frozen=True)
class RowAlignedDiff:
    """A 1:1 stage's output vs its input: per-column tallies plus the first rows as cells."""

    kind: ClassVar[str] = ROW_ALIGNED_KIND

    input_id: str
    columns: list[DiffColumn]
    rows: list[list[DiffCell]]
    rows_total: int
    changed_cells_total: int
    added_column_names: list[str]
    removed_column_names: list[str]

    @property
    def changed_columns(self) -> list[DiffColumn]:
        """The columns whose cells the stage changed, for the summary line."""
        return [column for column in self.columns if column.changed_cells]


@dataclass(frozen=True)
class DroppedRow:
    """One input row a filter dropped: its input ordinal and its rendered cells."""

    ordinal: int
    cells: list[str]


@dataclass(frozen=True)
class DroppedRowsDiff:
    """A filter_rows stage's drop report: which input rows its output no longer carries."""

    kind: ClassVar[str] = DROPPED_ROWS_KIND

    input_id: str
    columns: list[str]
    dropped: list[DroppedRow]
    dropped_total: int
    kept_total: int
    input_total: int


StageDiff = Union[RowAlignedDiff, DroppedRowsDiff]


def build_stage_diff(
    stage_def: Optional[Stage],
    run_dir: Path,
    output_path: Optional[str],
    output_by_id: dict[str, Optional[str]],
) -> Optional[StageDiff]:
    """The Outputs-pane diff for one executed stage — None wherever no honest diff exists."""
    if stage_def is None or len(stage_def.input_ids) != 1:
        return None
    if stage_def.type not in ROW_ALIGNED_TYPES and stage_def.type != StageType.filter_rows:
        return None
    input_id = stage_def.input_ids[0]
    input_df = _read_frame(run_dir, output_by_id.get(input_id))
    output_df = _read_frame(run_dir, output_path)
    if input_df is None or output_df is None:
        return None
    if stage_def.type == StageType.filter_rows:
        return _build_dropped_rows_diff(stage_def.id, input_id, run_dir, input_df, output_df)
    return _build_row_aligned_diff(input_id, input_df, output_df)


def _build_row_aligned_diff(
    input_id: str, input_df: pd.DataFrame, output_df: pd.DataFrame
) -> Optional[RowAlignedDiff]:
    """Positional cell diff of two same-length frames; None when the lengths differ."""
    if len(input_df) != len(output_df):
        return None
    in_text = _text_frame(input_df)
    out_text = _text_frame(output_df)
    input_names = set(in_text.columns)
    output_names = set(out_text.columns)
    columns = [
        DiffColumn(
            name=name,
            added=name not in input_names,
            changed_cells=_count_changed_cells(in_text, out_text, name),
        )
        for name in out_text.columns
    ]
    return RowAlignedDiff(
        input_id=input_id,
        columns=columns,
        rows=_shape_aligned_rows(in_text, out_text, columns),
        rows_total=len(output_df),
        changed_cells_total=sum(column.changed_cells for column in columns),
        added_column_names=[column.name for column in columns if column.added],
        removed_column_names=[name for name in in_text.columns if name not in output_names],
    )


def _count_changed_cells(in_text: pd.DataFrame, out_text: pd.DataFrame, name: str) -> int:
    """How many cells of a carried-through column differ, over the WHOLE frame."""
    if name not in in_text.columns:
        return 0
    return int((in_text[name] != out_text[name]).sum())


def _shape_aligned_rows(
    in_text: pd.DataFrame, out_text: pd.DataFrame, columns: list[DiffColumn]
) -> list[list[DiffCell]]:
    """The first ALIGNED_ROWS_SHOWN output rows as cells marked against their input row."""
    rows: list[list[DiffCell]] = []
    for i in range(min(len(out_text), ALIGNED_ROWS_SHOWN)):
        row: list[DiffCell] = []
        for column in columns:
            text = str(out_text[column.name].iat[i])
            if column.added:
                row.append(DiffCell(text=text, was=None, changed=False, added=True))
                continue
            was = str(in_text[column.name].iat[i])
            changed = was != text
            row.append(DiffCell(text=text, was=was if changed else None,
                                changed=changed, added=False))
        rows.append(row)
    return rows


def _build_dropped_rows_diff(
    stage_id: str, input_id: str, run_dir: Path,
    input_df: pd.DataFrame, output_df: pd.DataFrame,
) -> Optional[DroppedRowsDiff]:
    """The rows a filter dropped, per its lineage sidecar; None when the sidecar can't vouch."""
    kept = _read_kept_ordinals(
        run_dir, stage_id, input_id, rows_out=len(output_df), rows_in=len(input_df)
    )
    if kept is None:
        return None
    in_text = _text_frame(input_df)
    dropped_ordinals = [i for i in range(len(input_df)) if i not in kept]
    dropped = [
        DroppedRow(ordinal=i, cells=[str(in_text[name].iat[i]) for name in in_text.columns])
        for i in dropped_ordinals[:DROPPED_ROWS_SHOWN]
    ]
    return DroppedRowsDiff(
        input_id=input_id,
        columns=[str(name) for name in in_text.columns],
        dropped=dropped,
        dropped_total=len(dropped_ordinals),
        kept_total=len(output_df),
        input_total=len(input_df),
    )


def _read_kept_ordinals(
    run_dir: Path, stage_id: str, input_id: str, *, rows_out: int, rows_in: int
) -> Optional[set[int]]:
    """The input ordinals the stage kept, off its sidecar; None where alignment is unverifiable."""
    path = lineage_sidecar_path(run_dir, stage_id)
    if not path.exists():
        return None
    try:
        lineage = pd.read_parquet(path)
    except (OSError, ValueError):
        return None
    if len(lineage) != rows_out:
        return None
    if not bool((lineage[TRACE_SOURCE_STAGE_KEY] == input_id).all()):
        return None
    kept = {int(ordinal) for ordinal in lineage[TRACE_SOURCE_ROW_KEY]}
    if any(ordinal < 0 or ordinal >= rows_in for ordinal in kept):
        return None
    return kept


def _read_frame(run_dir: Path, rel_path: Optional[str]) -> Optional[pd.DataFrame]:
    """The persisted frame at `rel_path` under `run_dir`, or None where it cannot be read."""
    if not rel_path:
        return None
    path = run_dir / rel_path
    if not path.exists():
        return None
    try:
        return read_table(path)
    except (OSError, ValueError):
        # An unreadable frame means fallback to the plain output view, whose own
        # loader reports the read error in the pane — nothing is hidden here.
        return None


def _text_frame(df: pd.DataFrame) -> pd.DataFrame:
    """`df` as rendered strings under str column names — the form the panel's tables show."""
    text = df.fillna("").astype(str)
    text.columns = [str(name) for name in text.columns]
    return text
