"""Stage-aware diff for the run stage panel and the full-rows page: a 1:1
stage's INPUT frame as the base, with what the stage did to it painted over —
cells changed, columns dropped (still drawn, carrying the input value) or added
— and a filter stage's dropped rows read off its lineage sidecar. Anything
unverifiable yields None and the caller shows the plain output view."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import ClassVar, Optional, Union

import pandas as pd

from app.models import StageType, WorkflowStage
from app.models.stages.signature import list_read_column_names
from app.models.stage import is_grain_and_order_preserving
from app.runtime.lineage_sidecar import read_lineage_sidecar
from app.runtime.manifest import resolve_output_path
from app.core.frames import read_frame_file
from app.web.column_order import (
    ColumnGroup,
    find_column_group,
    order_columns_by_group,
)
from app.web.diff_state import CellDiffState, ColumnDiffState
from app.web.loading import PREVIEW_ROWS_SHOWN, render_frame_as_text

# The one grain-and-order-preserving type with nothing for a positional diff to
# say: input_data originates its rows, so there is no input frame to compare
# against. Every other one has an input and gets diffed — including
# human_review_queue, whose reviewed value lands in a column the stage ADDS
# (QueueConfig.reviewed_columns maps source -> added column), leaving the source
# column carried beside it. That reads as `+4 cols · 0 cells changed`, with the
# human's answer next to what it was answering.
_NO_ALIGNED_DIFF: frozenset[StageType] = frozenset({StageType.input_data})

# The 1:1-by-position stage types the aligned diff covers: their runtime contract
# maps output row i to input row i, so a positional comparison states facts.
# Read off the model's own grain-and-order fact rather than listed again here: a
# hand-kept second list is what left starlark_row_function with no diff for a
# release, and `validate_registry_matches_model` already holds the runtime
# handlers to that same fact at import, so what this pane assumes is what the
# executor enforces.
#
# enrich is added on top: it takes two inputs, so it is not grain-preserving as a
# TYPE, but its left merge runs under pandas' validate="m:1", which VERIFIES the
# reference holds at most one row per key — every subject row comes out once, in
# input order. expand (m:n fan-out) has no such guarantee and stays out.
ROW_ALIGNED_TYPES: frozenset[StageType] = frozenset(
    stage_type for stage_type in StageType
    if is_grain_and_order_preserving(stage_type) and stage_type not in _NO_ALIGNED_DIFF
) | {StageType.enrich}

# The default row budget is the panel's shared one (app.web.loading), so a diffed
# stage and an undiffed one draw the same depth. Callers with more room (the
# full-rows page) pass their own. Counts in the header always cover the whole
# frame; only the rows drawn are capped — aligned windows the OUTPUT frame,
# filter windows the INPUT frame, so dropped rows appear in place among the kept.

# All three emit a subsequence of their input, so one view serves them all.
FILTER_TYPES: frozenset[StageType] = frozenset(
    {StageType.filter_rows, StageType.starlark_filter_rows, StageType.dedupe}
)

ROW_ALIGNED_KIND = "row_aligned"
FILTER_ROWS_KIND = "filter_rows"

# U+2212, not the hyphen: the count sits beside `+` in the same line of text.
MINUS = "−"

# What the header calls each input frame. The BASE is the frame the table below
# IS, annotated; a REFERENCE is joined in and shares no row alignment with it.
# A stage with one input gets neither word — there is nothing to tell apart.
SOLE_INPUT_ROLE = "input"
BASE_INPUT_ROLE = "base input"
REFERENCE_INPUT_ROLE = "reference input"


@dataclass(frozen=True)
class DiffFrame:
    stage_id: str
    role: str
    # None wherever the frame was not read — a reference frame the diff never
    # needed and could not open. The header then shows no count for it; a count
    # is only ever a number counted off a frame.
    rows_total: Optional[int]


@dataclass(frozen=True)
class DiffCell:
    text: str
    was: Optional[str]
    state: CellDiffState


@dataclass(frozen=True)
class DiffColumn:
    name: str
    state: ColumnDiffState
    changed_cells: int
    # The stage's signature says the transform consumes it.
    read: bool = False

    @property
    def group(self) -> ColumnGroup:
        return find_column_group(self.state.value, read=self.read,
                                 changed=bool(self.changed_cells))

    @property
    def inert(self) -> bool:
        """Nothing happened to it here, and nothing here read it."""
        return self.group is ColumnGroup.untouched


@dataclass(frozen=True)
class RowAlignedDiff:
    kind: ClassVar[str] = ROW_ALIGNED_KIND

    inputs: list[DiffFrame]
    columns: list[DiffColumn]
    rows: list[list[DiffCell]]
    # Positional against `rows`: where in the frame each one was drawn from.
    row_ordinals: list[int]
    rows_total: int
    changed_cells_total: int
    added_column_names: list[str]
    removed_column_names: list[str]

    @property
    def output_rows(self) -> int:
        return self.rows_total

    @property
    def opens_on_the_first(self) -> bool:
        return opens_on_the_first(self.row_ordinals)

    @property
    def count_labels(self) -> list[str]:
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
    input_ordinal: int
    output_ordinal: Optional[int]
    cells: list[str]

    @property
    def dropped(self) -> bool:
        return self.output_ordinal is None


@dataclass(frozen=True)
class FilterRowsDiff:
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
    def count_labels(self) -> list[str]:
        if not self.dropped_total:
            return [_render_count(0, "row") + " dropped"]
        return [MINUS + _render_count(self.dropped_total, "row")]


StageDiff = Union[RowAlignedDiff, FilterRowsDiff]


def build_stage_diff(
    workflow_stage: Optional[WorkflowStage],
    run_dir: Path,
    output_path: Optional[str],
    output_by_id: dict[str, Optional[str]],
    rows_shown: int = PREVIEW_ROWS_SHOWN,
    at_rows: Optional[Sequence[int]] = None,
) -> Optional[StageDiff]:
    """`at_rows` names which rows to draw; without it the table opens on the first."""
    if workflow_stage is None:
        return None
    stage_def = workflow_stage.stage
    input_ids = _resolve_diff_input_ids(workflow_stage)
    if input_ids is None:
        return None
    input_df = _read_frame(run_dir, output_by_id.get(input_ids[0]))
    output_df = _read_frame(run_dir, output_path)
    if input_df is None or output_df is None:
        return None
    inputs = _shape_input_frames(run_dir, input_ids, output_by_id, len(input_df))
    drawn = _choose_rows(len(output_df), rows_shown, at_rows)
    if stage_def.type in FILTER_TYPES:
        return _build_filter_rows_diff(
            stage_def.id, inputs, run_dir, input_df, output_df, rows_shown
        )
    return _build_row_aligned_diff(workflow_stage, inputs, input_df, output_df, drawn)


def _choose_rows(
    rows_total: int, rows_shown: int, at_rows: Optional[Sequence[int]]
) -> list[int]:
    if at_rows is None:
        return list(range(min(rows_total, rows_shown)))
    return [row for row in at_rows if 0 <= row < rows_total][:rows_shown]


def opens_on_the_first(row_ordinals: Sequence[int]) -> bool:
    """False once rows were picked, where "the first N" would misname them."""
    return list(row_ordinals) == list(range(len(row_ordinals)))


def keep_diff_columns(diff: RowAlignedDiff, names: Collection[str]) -> RowAlignedDiff:
    """Cuts what the table DRAWS; the counts stay the whole frame's."""
    kept = [i for i, column in enumerate(diff.columns) if column.name in names]
    return replace(
        diff,
        columns=[diff.columns[i] for i in kept],
        rows=[[row[i] for i in kept] for row in diff.rows],
    )


def _shape_input_frames(
    run_dir: Path, input_ids: list[str], output_by_id: dict[str, Optional[str]], base_rows: int
) -> list[DiffFrame]:
    if len(input_ids) == 1:
        return [DiffFrame(stage_id=input_ids[0], role=SOLE_INPUT_ROLE, rows_total=base_rows)]
    return [DiffFrame(stage_id=input_ids[0], role=BASE_INPUT_ROLE, rows_total=base_rows)] + [
        _shape_reference_frame(run_dir, output_by_id.get(input_id), input_id)
        for input_id in input_ids[1:]
    ]


def _shape_reference_frame(
    run_dir: Path, rel_path: Optional[str], stage_id: str
) -> DiffFrame:
    frame = _read_frame(run_dir, rel_path)
    return DiffFrame(
        stage_id=stage_id,
        role=REFERENCE_INPUT_ROLE,
        rows_total=None if frame is None else len(frame),
    )


def _resolve_diff_input_ids(workflow_stage: WorkflowStage) -> Optional[list[str]]:
    stage_def = workflow_stage.stage
    if stage_def.type not in ROW_ALIGNED_TYPES and stage_def.type not in FILTER_TYPES:
        return None
    # enrich takes two inputs and diffs against inputs[0], its SUBJECT: that is
    # the frame its output is row-aligned with, while inputs[1] is a reference
    # the output shares no alignment with. Every other covered type takes one.
    expected_inputs = 2 if stage_def.type == StageType.enrich else 1
    if len(stage_def.input_ids) != expected_inputs:
        return None
    return list(stage_def.input_ids)


def _build_row_aligned_diff(
    workflow_stage: WorkflowStage, inputs: list[DiffFrame],
    input_df: pd.DataFrame, output_df: pd.DataFrame, drawn: list[int],
) -> Optional[RowAlignedDiff]:
    if len(input_df) != len(output_df):
        return None
    in_text = _text_frame(input_df)
    out_text = _text_frame(output_df)
    columns = order_columns_by_group(
        _shape_aligned_columns(
            in_text, out_text, list_read_column_names(workflow_stage.stage)))
    return RowAlignedDiff(
        inputs=inputs,
        columns=columns,
        rows=_shape_aligned_rows(in_text, out_text, columns, drawn),
        row_ordinals=drawn,
        rows_total=len(output_df),
        changed_cells_total=sum(column.changed_cells for column in columns),
        added_column_names=[
            column.name for column in columns if column.state is ColumnDiffState.added
        ],
        removed_column_names=[
            column.name for column in columns if column.state is ColumnDiffState.dropped
        ],
    )


def _shape_aligned_columns(
    in_text: pd.DataFrame, out_text: pd.DataFrame, read: set[str]
) -> list[DiffColumn]:
    input_names = set(in_text.columns)
    return [
        _shape_input_column(in_text, out_text, str(name), read) for name in in_text.columns
    ] + [
        DiffColumn(name=str(name), state=ColumnDiffState.added, changed_cells=0)
        for name in out_text.columns
        if name not in input_names
    ]


def _shape_input_column(
    in_text: pd.DataFrame, out_text: pd.DataFrame, name: str, read: set[str]
) -> DiffColumn:
    if name not in out_text.columns:
        return DiffColumn(name=name, state=ColumnDiffState.dropped, changed_cells=0)
    return DiffColumn(
        name=name,
        state=ColumnDiffState.carried,
        changed_cells=int((in_text[name] != out_text[name]).sum()),
        read=name in read,
    )


def _shape_aligned_rows(
    in_text: pd.DataFrame, out_text: pd.DataFrame, columns: list[DiffColumn], drawn: list[int]
) -> list[list[DiffCell]]:
    in_values = _take_column_lists(in_text, drawn)
    # Column-major, once per frame: reading each cell back as frame[name].iat[i] inside
    # the loop re-resolved its column every time — ~18µs a cell, and a 5,000-row export
    # shapes half a million of them.
    out_values = _take_column_lists(out_text, drawn)
    return [
        [_shape_aligned_cell(in_values, out_values, column, i) for column in columns]
        for i in range(len(drawn))
    ]


def _take_column_lists(text: pd.DataFrame, drawn: list[int]) -> dict[str, list[str]]:
    """Values, not cells: `text` is already all-string, so `_text_frame` did the str()."""
    return {name: text[name].take(drawn).tolist() for name in text.columns}


def _shape_aligned_cell(
    in_values: dict[str, list[str]],
    out_values: dict[str, list[str]],
    column: DiffColumn,
    i: int,
) -> DiffCell:
    if column.state is ColumnDiffState.dropped:
        # No output value exists, so the cell shows what the stage discarded —
        # the whole point of drawing the column at all.
        return DiffCell(text=in_values[column.name][i], was=None,
                        state=CellDiffState.dropped)
    text = out_values[column.name][i]
    if column.state is ColumnDiffState.added:
        return DiffCell(text=text, was=None, state=CellDiffState.added)
    was = in_values[column.name][i]
    if was == text:
        return DiffCell(text=text, was=None, state=CellDiffState.carried)
    return DiffCell(text=text, was=was, state=CellDiffState.changed)


def _build_filter_rows_diff(
    stage_id: str, inputs: list[DiffFrame], run_dir: Path,
    input_df: pd.DataFrame, output_df: pd.DataFrame, rows_shown: int,
) -> Optional[FilterRowsDiff]:
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
    output_ordinal_by_input = {
        input_ordinal: output_ordinal for output_ordinal, input_ordinal in enumerate(kept)
    }
    window = min(len(in_text), rows_shown)
    in_values = _take_column_lists(in_text, list(range(window)))
    # The whole output, not the window: a kept row's ordinal indexes the output
    # frame, and the last row of an input window can sit anywhere in it.
    out_values = _take_column_lists(out_text, list(range(len(out_text))))
    names = list(in_text.columns)
    rows: list[FilterRow] = []
    for i in range(window):
        output_ordinal = output_ordinal_by_input.get(i)
        # A kept row's cells come from the persisted OUTPUT row — the thing this
        # pane shows — not from the input copy the pass-through contract implies.
        source, at = (in_values, i) if output_ordinal is None else (out_values, output_ordinal)
        rows.append(FilterRow(
            input_ordinal=i,
            output_ordinal=output_ordinal,
            cells=[source[name][at] for name in names],
        ))
    return rows


def _read_kept_ordinals(
    run_dir: Path, stage_id: str, input_id: str, *, rows_out: int, rows_in: int
) -> Optional[list[int]]:
    try:
        lineage = read_lineage_sidecar(run_dir, stage_id).lineage
    except (OSError, ValueError):
        return None
    if lineage is None:
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
    if not rel_path:
        return None
    try:
        path = resolve_output_path(run_dir, rel_path)
        if path is None or not path.exists():
            return None
        return read_frame_file(path)
    except (OSError, ValueError):
        # An unreadable frame means fallback to the plain output view, whose own
        # loader reports the read error in the pane — nothing is hidden here.
        return None


def _render_count(count: int, noun: str) -> str:
    return f"{count:,} {noun}{'' if count == 1 else 's'}"


def _text_frame(df: pd.DataFrame) -> pd.DataFrame:
    text = render_frame_as_text(df)
    text.columns = [str(name) for name in text.columns]
    return text
