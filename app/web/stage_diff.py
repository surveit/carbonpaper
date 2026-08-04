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

# One row budget for both rendered tables. Counts in the header always cover
# the whole frame; only the rows drawn are capped — aligned: the first-5 window
# the plain output preview showed; filter: the same window over the INPUT
# frame, so dropped rows appear in place among the kept ones.
DIFF_ROWS_SHOWN = 5

ROW_ALIGNED_KIND = "row_aligned"
FILTER_ROWS_KIND = "filter_rows"


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
class FilterRow:
    """One input row of the merged filter table: kept (with its output ordinal) or dropped."""

    input_ordinal: int
    output_ordinal: Optional[int]
    cells: list[str]

    @property
    def dropped(self) -> bool:
        """Whether the filter dropped this input row (it carries no output ordinal)."""
        return self.output_ordinal is None


@dataclass(frozen=True)
class FilterRowsDiff:
    """A filter_rows stage's output with the dropped input rows shown in place, in input order."""

    kind: ClassVar[str] = FILTER_ROWS_KIND

    input_id: str
    columns: list[str]
    rows: list[FilterRow]
    input_total: int
    kept_total: int
    dropped_total: int
    dropped_beyond_window: int


StageDiff = Union[RowAlignedDiff, FilterRowsDiff]


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
        return _build_filter_rows_diff(stage_def.id, input_id, run_dir, input_df, output_df)
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
    """The first DIFF_ROWS_SHOWN output rows as cells marked against their input row."""
    rows: list[list[DiffCell]] = []
    for i in range(min(len(out_text), DIFF_ROWS_SHOWN)):
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


def _build_filter_rows_diff(
    stage_id: str, input_id: str, run_dir: Path,
    input_df: pd.DataFrame, output_df: pd.DataFrame,
) -> Optional[FilterRowsDiff]:
    """The merged filter table off the verified sidecar; None where it can't vouch for alignment."""
    kept = _read_kept_ordinals(
        run_dir, stage_id, input_id, rows_out=len(output_df), rows_in=len(input_df)
    )
    if kept is None:
        return None
    in_text = _text_frame(input_df)
    out_text = _text_frame(output_df)
    if list(in_text.columns) != list(out_text.columns):
        # A filter passes columns through unchanged; frames that disagree mean
        # the alignment story doesn't hold, so no merged table.
        return None
    rows = _shape_filter_rows(in_text, out_text, kept)
    dropped_total = len(input_df) - len(output_df)
    dropped_in_window = sum(1 for row in rows if row.dropped)
    return FilterRowsDiff(
        input_id=input_id,
        columns=[str(name) for name in in_text.columns],
        rows=rows,
        input_total=len(input_df),
        kept_total=len(output_df),
        dropped_total=dropped_total,
        dropped_beyond_window=dropped_total - dropped_in_window,
    )


def _shape_filter_rows(
    in_text: pd.DataFrame, out_text: pd.DataFrame, kept: list[int]
) -> list[FilterRow]:
    """The first DIFF_ROWS_SHOWN INPUT rows, each kept (drawn from the output) or dropped."""
    output_ordinal_by_input = {
        input_ordinal: output_ordinal for output_ordinal, input_ordinal in enumerate(kept)
    }
    rows: list[FilterRow] = []
    for i in range(min(len(in_text), DIFF_ROWS_SHOWN)):
        output_ordinal = output_ordinal_by_input.get(i)
        # A kept row's cells come from the persisted OUTPUT row — the thing this
        # pane shows — not from the input copy the pass-through contract implies.
        source, at = (in_text, i) if output_ordinal is None else (out_text, output_ordinal)
        cells = [str(source[name].iat[at]) for name in source.columns]
        rows.append(FilterRow(input_ordinal=i, output_ordinal=output_ordinal, cells=cells))
    return rows


def _read_kept_ordinals(
    run_dir: Path, stage_id: str, input_id: str, *, rows_out: int, rows_in: int
) -> Optional[list[int]]:
    """The input ordinals the stage kept, in output order; None where alignment is unverifiable."""
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
    kept = [int(ordinal) for ordinal in lineage[TRACE_SOURCE_ROW_KEY]]
    if any(ordinal < 0 or ordinal >= rows_in for ordinal in kept):
        return None
    if any(later <= earlier for earlier, later in zip(kept, kept[1:])):
        # A filter emits a subsequence of its input, so the kept ordinals must
        # strictly increase — anything else is not a filter's sidecar.
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
