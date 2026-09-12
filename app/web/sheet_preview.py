"""The sheet under each canvas box: a few of the stage's rows, the figure's own leading."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pyarrow as pa

from app.core.frames import convert_cell_to_json_value, read_frame_table
from app.core.json_types import JsonScalar
from app.models import StageType
from app.models.branch_analysis import RowOrdinal
from app.models.run_manifest import StageRecord
from app.models.schema import StageId
from app.runtime.branch_analysis import WorkflowRunBranches
from app.runtime.manifest import resolve_output_path
from app.services.workspace import resolve_run_dir
from app.web.stage_diff import FILTER_TYPES, FilterRowsDiff, build_stage_diff
from app.web.values_payload import CanvasSheet, MinimapCut, SheetRow

PREVIEW_ROWS = 8
CELL_CHARS = 60
# A filter's dropped rows sit among its kept ones, so its window is read deeper.
FILTER_WINDOW = PREVIEW_ROWS * 4

OutputPathByStage = dict[StageId, str | None]
ColumnsBehindByStage = dict[StageId, set[str]]


def build_canvas_sheets(project_id: str, run_id: str, run_branches: WorkflowRunBranches,
                        reached: dict[StageId, set[RowOrdinal]],
                        stage_records: list[StageRecord],
                        cuts: list[MinimapCut],
                        columns_behind: ColumnsBehindByStage) -> list[CanvasSheet]:
    run_dir = resolve_run_dir(project_id, run_id)
    output_by_id: OutputPathByStage = {
        record.stage_id: record.output_path for record in stage_records}
    dropped = _count_dropped_per_stage(cuts)
    sheets = []
    for stage_id in run_branches.ordered_stage_ids:
        frame_path = resolve_output_path(run_dir, output_by_id[stage_id])
        if frame_path is None or not frame_path.exists():
            continue
        sheets.append(_build_sheet(
            run_branches, stage_id, run_dir, frame_path, output_by_id,
            mine=sorted(reached.get(stage_id, ())), rows_dropped=dropped[stage_id],
            behind=columns_behind[stage_id]))
    return sheets


def render_sheet_cell(value: JsonScalar) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= CELL_CHARS else text[:CELL_CHARS - 1] + "…"


def _count_dropped_per_stage(cuts: list[MinimapCut]) -> Counter[StageId]:
    dropped: Counter[StageId] = Counter()
    for cut in cuts:
        dropped[cut.stage_id] += cut.rows
    return dropped


def _build_sheet(run_branches: WorkflowRunBranches, stage_id: StageId, run_dir: Path,
                 frame_path: Path, output_by_id: OutputPathByStage,
                 mine: list[RowOrdinal], rows_dropped: int,
                 behind: set[str]) -> CanvasSheet:
    workflow_stage = run_branches.stages[stage_id]
    authored = workflow_stage.stage
    frame = read_frame_table(frame_path)
    diff = None
    if authored.type in FILTER_TYPES:
        diff = build_stage_diff(workflow_stage, run_dir, output_by_id[stage_id],
                                output_by_id, rows_shown=FILTER_WINDOW)
    if isinstance(diff, FilterRowsDiff):
        rows_dropped = _read_dropped_off_the_diff(stage_id, diff, rows_dropped)
    return CanvasSheet(
        stage_id=stage_id, type=StageType(authored.type).value,
        rows_in=frame.num_rows + rows_dropped, rows_out=frame.num_rows,
        rows_dropped=rows_dropped, rows_behind=len(mine),
        columns=list(frame.column_names), rows=_choose_rows(frame, mine),
        columns_behind=[name for name in frame.column_names if name in behind])


# Off the figure's route no cut is recorded, so the diff's count is the one that holds.
def _read_dropped_off_the_diff(stage_id: StageId, diff: FilterRowsDiff,
                               from_cuts: int) -> int:
    if from_cuts and from_cuts != diff.dropped_total:
        raise ValueError(
            f"stage '{stage_id}': its cut records {from_cuts} dropped rows, "
            f"its diff {diff.dropped_total}")
    return diff.dropped_total


# A stage none of the figure's rows reached shows the frame's first instead.
def _choose_rows(frame: pa.Table, mine: list[RowOrdinal]) -> list[SheetRow]:
    if mine:
        return _read_rows(frame, mine[:PREVIEW_ROWS], mine=True)
    first = list(range(min(frame.num_rows, PREVIEW_ROWS)))
    return _read_rows(frame, first, mine=False)


def _read_rows(frame: pa.Table, ordinals: list[RowOrdinal], mine: bool) -> list[SheetRow]:
    taken = frame.take(pa.array(ordinals, type=pa.int64()))
    columns = [[render_sheet_cell(convert_cell_to_json_value(value))
                for value in taken.column(name).to_pylist()]
               for name in taken.column_names]
    return [SheetRow(ordinal=ordinal, cells=[column[position] for column in columns],
                     mine=mine, dropped=False)
            for position, ordinal in enumerate(ordinals)]
