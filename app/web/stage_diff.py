"""Stage-aware diff for the run stage panel and the full-rows page: a 1:1
stage's INPUT frame as the base, with what the stage did to it painted over —
cells changed, columns dropped (still drawn, carrying the input value) or added
— and a filter_rows stage's dropped rows read off its lineage sidecar. Anything
unverifiable yields None and the caller shows the plain output view."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import ClassVar, Optional, Union

import pandas as pd

from app.core.frames import render_frame_as_text
from app.models import Stage, StageType
from app.runtime.lineage import RowLineage, lineage_sidecar_path
from app.web.loading import read_table

# The 1:1-by-position stage types the aligned diff covers: their runtime
# contract maps output row i to input row i, so a positional comparison states
# facts. enrich qualifies because its left merge runs under pandas'
# validate="m:1", which VERIFIES the reference holds at most one row per key —
# so every subject row comes out once, in input order. expand (m:n fan-out) does
# not. Deliberately NOT is_grain_and_order_preserving(), which also admits
# input_data (no input to diff against) and human_review_queue (out of scope).
ROW_ALIGNED_TYPES: frozenset[StageType] = frozenset({
    StageType.python_row_function,
    StageType.llm_transform,
    StageType.enrich,
})

# The default row budget: the window the stage panel draws, deep enough to read
# a stage rather than sample it. Callers with more room (the full-rows page) pass
# their own. Counts in the header always cover the whole frame; only the rows
# drawn are capped — aligned windows the OUTPUT frame, filter windows the INPUT
# frame, so dropped rows appear in place among the kept ones.
DIFF_ROWS_SHOWN = 100

ROW_ALIGNED_KIND = "row_aligned"
FILTER_ROWS_KIND = "filter_rows"

# U+2212, not the hyphen: the tally sets it beside `+` in the same line of text.
MINUS = "−"

# What the header calls each input frame. The BASE is the frame the table below
# IS, annotated; a REFERENCE is joined in and shares no row alignment with it.
# A stage with one input gets neither word — there is nothing to tell apart.
SOLE_INPUT_ROLE = "input"
BASE_INPUT_ROLE = "base input"
REFERENCE_INPUT_ROLE = "reference input"


class ColumnDiffState(str, Enum):
    """What the stage did to one column of the base: kept it, dropped it, or invented it."""

    carried = "carried"
    dropped = "dropped"
    added = "added"


class CellDiffState(str, Enum):
    """A cell's state — its column's, with a carried column's cells split by whether it changed."""

    carried = "carried"
    changed = "changed"
    dropped = "dropped"
    added = "added"


@dataclass(frozen=True)
class DiffFrame:
    """One input frame of the header: its part in the diff, and its row count where read."""

    stage_id: str
    role: str
    # None wherever the frame was not read — a reference frame the diff never
    # needed and could not open. The header then shows no count for it; a count
    # is only ever a number counted off a frame.
    rows_total: Optional[int]


@dataclass(frozen=True)
class DiffCell:
    """One cell of the table: its text, and the input value it replaced (if any)."""

    text: str
    was: Optional[str]
    state: CellDiffState


@dataclass(frozen=True)
class DiffColumn:
    """One table column: what the stage did to it, and its whole-frame changed-cell count."""

    name: str
    state: ColumnDiffState
    changed_cells: int


@dataclass(frozen=True)
class RowAlignedDiff:
    """A 1:1 stage's output painted over its input: per-column tallies plus the first rows."""

    kind: ClassVar[str] = ROW_ALIGNED_KIND

    inputs: list[DiffFrame]
    columns: list[DiffColumn]
    rows: list[list[DiffCell]]
    rows_total: int
    changed_cells_total: int
    added_column_names: list[str]
    removed_column_names: list[str]

    @property
    def output_rows(self) -> int:
        return self.rows_total

    @property
    def tally(self) -> list[str]:
        """What the stage did to the frame — the three things a positional diff measures."""
        parts = []
        if self.added_column_names:
            parts.append("+" + _render_count(len(self.added_column_names), "col"))
        if self.removed_column_names:
            parts.append(MINUS + _render_count(len(self.removed_column_names), "col"))
        # Always stated: a positional diff compares every carried cell, so zero
        # here is a count it took, not a metric it skipped.
        parts.append(_render_count(self.changed_cells_total, "cell") + " changed")
        return parts


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

    inputs: list[DiffFrame]
    columns: list[str]
    rows: list[FilterRow]
    input_total: int
    kept_total: int
    dropped_total: int
    dropped_beyond_window: int

    @property
    def output_rows(self) -> int:
        return self.kept_total

    @property
    def tally(self) -> list[str]:
        """What the stage did to the frame — rows only; a filter measures no cell and no column."""
        if not self.dropped_total:
            return [_render_count(0, "row") + " dropped"]
        return [MINUS + _render_count(self.dropped_total, "row")]


StageDiff = Union[RowAlignedDiff, FilterRowsDiff]


def build_stage_diff(
    stage_def: Optional[Stage],
    run_dir: Path,
    output_path: Optional[str],
    output_by_id: dict[str, Optional[str]],
    rows_shown: int = DIFF_ROWS_SHOWN,
) -> Optional[StageDiff]:
    """The diff for one executed stage over `rows_shown` rows — None wherever none is honest."""
    if stage_def is None:
        return None
    input_ids = _resolve_diff_input_ids(stage_def)
    if input_ids is None:
        return None
    input_df = _read_frame(run_dir, output_by_id.get(input_ids[0]))
    output_df = _read_frame(run_dir, output_path)
    if input_df is None or output_df is None:
        return None
    inputs = _shape_input_frames(run_dir, input_ids, output_by_id, len(input_df))
    if stage_def.type == StageType.filter_rows:
        return _build_filter_rows_diff(
            stage_def.id, inputs, run_dir, input_df, output_df, rows_shown
        )
    return _build_row_aligned_diff(inputs, input_df, output_df, rows_shown)


def _shape_input_frames(
    run_dir: Path, input_ids: list[str], output_by_id: dict[str, Optional[str]], base_rows: int
) -> list[DiffFrame]:
    """The header's input units: the base, already read, then every reference frame."""
    if len(input_ids) == 1:
        return [DiffFrame(stage_id=input_ids[0], role=SOLE_INPUT_ROLE, rows_total=base_rows)]
    return [DiffFrame(stage_id=input_ids[0], role=BASE_INPUT_ROLE, rows_total=base_rows)] + [
        _shape_reference_frame(run_dir, output_by_id.get(input_id), input_id)
        for input_id in input_ids[1:]
    ]


def _shape_reference_frame(
    run_dir: Path, rel_path: Optional[str], stage_id: str
) -> DiffFrame:
    """A frame the diff only links: counted by reading it, or left uncounted where it will not read."""
    frame = _read_frame(run_dir, rel_path)
    return DiffFrame(
        stage_id=stage_id,
        role=REFERENCE_INPUT_ROLE,
        rows_total=None if frame is None else len(frame),
    )


def _resolve_diff_input_ids(stage_def: Stage) -> Optional[list[str]]:
    """The stage's inputs, the base — the frame the output is diffed against — first, or None."""
    if stage_def.type not in ROW_ALIGNED_TYPES and stage_def.type != StageType.filter_rows:
        return None
    # enrich takes two inputs and diffs against inputs[0], its SUBJECT: that is
    # the frame its output is row-aligned with, while inputs[1] is a reference
    # the output shares no alignment with. Every other covered type takes one.
    expected_inputs = 2 if stage_def.type == StageType.enrich else 1
    if len(stage_def.input_ids) != expected_inputs:
        return None
    return list(stage_def.input_ids)


def _build_row_aligned_diff(
    inputs: list[DiffFrame], input_df: pd.DataFrame, output_df: pd.DataFrame, rows_shown: int
) -> Optional[RowAlignedDiff]:
    """Positional cell diff of two same-length frames; None when the lengths differ."""
    if len(input_df) != len(output_df):
        return None
    in_text = _text_frame(input_df)
    out_text = _text_frame(output_df)
    columns = _shape_aligned_columns(in_text, out_text)
    return RowAlignedDiff(
        inputs=inputs,
        columns=columns,
        rows=_shape_aligned_rows(in_text, out_text, columns, rows_shown),
        rows_total=len(output_df),
        changed_cells_total=sum(column.changed_cells for column in columns),
        added_column_names=[
            column.name for column in columns if column.state is ColumnDiffState.added
        ],
        removed_column_names=[
            column.name for column in columns if column.state is ColumnDiffState.dropped
        ],
    )


def _shape_aligned_columns(in_text: pd.DataFrame, out_text: pd.DataFrame) -> list[DiffColumn]:
    """The INPUT columns in input order — the base the diff is painted over — then the added ones."""
    input_names = set(in_text.columns)
    return [
        _shape_input_column(in_text, out_text, str(name)) for name in in_text.columns
    ] + [
        DiffColumn(name=str(name), state=ColumnDiffState.added, changed_cells=0)
        for name in out_text.columns
        if name not in input_names
    ]


def _shape_input_column(in_text: pd.DataFrame, out_text: pd.DataFrame, name: str) -> DiffColumn:
    """One column of the base: carried through with its whole-frame changed count, or dropped."""
    if name not in out_text.columns:
        return DiffColumn(name=name, state=ColumnDiffState.dropped, changed_cells=0)
    return DiffColumn(
        name=name,
        state=ColumnDiffState.carried,
        changed_cells=int((in_text[name] != out_text[name]).sum()),
    )


def _shape_aligned_rows(
    in_text: pd.DataFrame, out_text: pd.DataFrame, columns: list[DiffColumn], rows_shown: int
) -> list[list[DiffCell]]:
    """The first `rows_shown` output rows as cells marked against their input row."""
    return [
        [_shape_aligned_cell(in_text, out_text, column, i) for column in columns]
        for i in range(min(len(out_text), rows_shown))
    ]


def _shape_aligned_cell(
    in_text: pd.DataFrame, out_text: pd.DataFrame, column: DiffColumn, i: int
) -> DiffCell:
    """Row `i` of one column: its output value against its input, or the dropped input value."""
    if column.state is ColumnDiffState.dropped:
        # No output value exists, so the cell shows what the stage discarded —
        # the whole point of drawing the column at all.
        return DiffCell(text=str(in_text[column.name].iat[i]), was=None,
                        state=CellDiffState.dropped)
    text = str(out_text[column.name].iat[i])
    if column.state is ColumnDiffState.added:
        return DiffCell(text=text, was=None, state=CellDiffState.added)
    was = str(in_text[column.name].iat[i])
    if was == text:
        return DiffCell(text=text, was=None, state=CellDiffState.carried)
    return DiffCell(text=text, was=was, state=CellDiffState.changed)


def _build_filter_rows_diff(
    stage_id: str, inputs: list[DiffFrame], run_dir: Path,
    input_df: pd.DataFrame, output_df: pd.DataFrame, rows_shown: int,
) -> Optional[FilterRowsDiff]:
    """The merged filter table off the verified sidecar; None where it can't vouch for alignment."""
    kept = _read_kept_ordinals(
        run_dir, stage_id, inputs[0].stage_id, rows_out=len(output_df), rows_in=len(input_df)
    )
    if kept is None:
        return None
    in_text = _text_frame(input_df)
    out_text = _text_frame(output_df)
    if list(in_text.columns) != list(out_text.columns):
        # A filter passes columns through unchanged; frames that disagree mean
        # the alignment story doesn't hold, so no merged table.
        return None
    rows = _shape_filter_rows(in_text, out_text, kept, rows_shown)
    dropped_total = len(input_df) - len(output_df)
    dropped_in_window = sum(1 for row in rows if row.dropped)
    return FilterRowsDiff(
        inputs=inputs,
        columns=[str(name) for name in in_text.columns],
        rows=rows,
        input_total=len(input_df),
        kept_total=len(output_df),
        dropped_total=dropped_total,
        dropped_beyond_window=dropped_total - dropped_in_window,
    )


def _shape_filter_rows(
    in_text: pd.DataFrame, out_text: pd.DataFrame, kept: list[int], rows_shown: int
) -> list[FilterRow]:
    """The first `rows_shown` INPUT rows, each kept (drawn from the output) or dropped."""
    output_ordinal_by_input = {
        input_ordinal: output_ordinal for output_ordinal, input_ordinal in enumerate(kept)
    }
    rows: list[FilterRow] = []
    for i in range(min(len(in_text), rows_shown)):
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
        lineage = RowLineage.from_frame(pd.read_parquet(path))
    except (OSError, ValueError):
        return None
    if len(lineage) != rows_out:
        return None
    # A filter's row has exactly one parent, in the input being diffed. A row
    # with two (a join) or none (an unmatched subject) is a different shape, and
    # this pane states nothing about it.
    if not all(len(entry) == 1 and entry[0].stage_id == input_id for entry in lineage.parents):
        return None
    kept = [entry[0].row_ordinal for entry in lineage.parents]
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


def _render_count(count: int, noun: str) -> str:
    """`1 col` / `2 cols` — a counted noun, agreeing in number and thousands-separated."""
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _text_frame(df: pd.DataFrame) -> pd.DataFrame:
    """`df` as rendered strings under str column names — the form the panel's tables show."""
    text = render_frame_as_text(df)
    text.columns = [str(name) for name in text.columns]
    return text
