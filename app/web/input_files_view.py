"""What the Input files tab shows: each file a figure read, sliced to what it needed."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Sequence

import pyarrow as pa
from pydantic import BaseModel

from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.frames import read_frame_table
from app.core.json_types import JsonScalar
from app.models.branch_analysis import RowOrdinal
from app.models.claims import StageOutputCellCitation
from app.models.schema import StageId
from app.models.stages.input_data import InputDataStage
from app.models.records.run_manifest import RunManifest
from app.models.workflow import Workflow
from app.runtime.manifest import read_run_manifest
from app.services import run as run_service
from app.services.input_check import SOURCE_ROW_COLUMN
from app.services.input_slice import find_columns_behind
from app.services.scope import find_rows_reached_per_stage
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir
from app.web.file_detail_view import ColumnRow, build_column_row
from app.web.file_sizes import describe_bytes
from app.web.scope_view import read_run_branches

# Rows shown beside the relevant ones when a reader widens the preview to the frame.
OTHER_ROWS_SHOWN = 40


class Basis(str, Enum):
    """Which rows, or which columns, every panel of the tab is about."""

    relevant = "relevant"
    all = "all"


class PreviewRow(BaseModel):
    """`label` is the line this row holds in the file, where the loader stamped one."""

    label: str
    relevant: bool
    cells: list[JsonScalar]


class InputFileSlice(BaseModel):
    stage_id: StageId
    filename: str
    size_label: str
    rows_relevant: int
    rows_read: int
    # None where nothing the run wrote says how many rows the file holds.
    rows_in_file: int | None
    cap: int | None
    columns_relevant: list[str]
    columns_read: list[str]
    shape_over_relevant_rows: list[ColumnRow]
    shape_over_every_row: list[ColumnRow]
    row_label: str
    rows: list[PreviewRow]
    ordinals: list[RowOrdinal]

    @property
    def read_percent(self) -> float:
        return _share(self.rows_read, self.rows_in_file or self.rows_read)

    @property
    def relevant_percent(self) -> float:
        return _share(self.rows_relevant, self.rows_in_file or self.rows_read)

    @property
    def columns_percent(self) -> float:
        return _share(len(self.columns_relevant), len(self.columns_read))


# Below this a bar reads as one that failed to draw.
NARROWEST_BAR = 0.5


def _share(part: int, whole: int) -> float:
    return max(part * 100 / whole, NARROWEST_BAR) if whole else 0.0


class InputFilesView(BaseModel):
    citation: StageOutputCellCitation
    value: JsonScalar
    files: list[InputFileSlice]


def load_input_files(project_id: str, run_id: str,
                     citation: StageOutputCellCitation) -> InputFilesView:
    branches = read_run_branches(project_id, run_id)
    reached = find_rows_reached_per_stage(
        branches, [(citation.stage_id, citation.row_ordinal)])
    workflow = Workflow(stages=load_version_stages(
        project_id, run_service.read_pinned_version(project_id, run_id)))
    behind = find_columns_behind(workflow, set(reached),
                                 citation.stage_id, citation.column)
    outputs = resolve_run_dir(project_id, run_id) / "outputs"
    manifest = read_run_manifest(project_id, run_id)
    reading = [placed.id for placed in workflow.list_workflow_stages()
               if isinstance(placed.stage, InputDataStage) and placed.id in reached]
    files = [_build_one_file(outputs, manifest, stage_id, sorted(reached[stage_id]),
                             sorted(behind.get(stage_id, ())))
             # The run's order, not the workflow's: the reader met these files in it.
             for stage_id in _in_the_order_the_run_read_them(manifest, reading)]
    return InputFilesView(citation=citation, files=files,
                          value=_read_the_cited_cell(outputs, citation))


def _in_the_order_the_run_read_them(manifest: RunManifest,
                                   stage_ids: Sequence[StageId]) -> list[StageId]:
    ran = [record.stage_id for record in manifest.stage_records]
    return sorted(stage_ids, key=lambda stage_id: (ran.index(stage_id)
                                                   if stage_id in ran else len(ran)))


def _build_one_file(outputs: Path, manifest: RunManifest, stage_id: StageId,
                    ordinals: Sequence[RowOrdinal],
                    relevant: Sequence[str]) -> InputFileSlice:
    frame = read_frame_table(outputs / f"{stage_id}.parquet")
    bound = _read_the_binding(manifest, stage_id)
    read = [str(name) for name in frame.column_names]
    return InputFileSlice(
        stage_id=stage_id,
        filename=Path(str(bound.get("path", ""))).name or stage_id,
        size_label=describe_bytes(int(bound.get("bytes") or 0)),
        rows_relevant=len(ordinals), rows_read=frame.num_rows,
        rows_in_file=_read_the_row_count_before_the_cut(manifest, stage_id,
                                                        frame.num_rows),
        cap=_read_the_cap(manifest, stage_id),
        columns_relevant=list(relevant), columns_read=read,
        shape_over_relevant_rows=_measure_shape(frame.take(pa.array(list(ordinals)))),
        shape_over_every_row=_measure_shape(frame),
        row_label=("sheet row" if SOURCE_ROW_COLUMN in read else "row"),
        rows=_build_preview(frame, ordinals),
        ordinals=list(ordinals),
    )


def _measure_shape(frame: pa.Table) -> list[ColumnRow]:
    rows = max(frame.num_rows, 1)
    return [build_column_row(_measure_one_column(frame, str(name)), rows)
            for name in frame.column_names]


def _measure_one_column(frame: pa.Table, column: str):
    cells = frame.column(column).to_pylist()
    filled = [str(cell) for cell in cells if cell is not None]
    return measure_column_shape(column, filled, null_count=len(cells) - len(filled),
                                max_values=VALUES_KEPT)


def _build_preview(frame: pa.Table, ordinals: Sequence[RowOrdinal]) -> list[PreviewRow]:
    """The relevant rows, then the head of the rest for a reader who widens the view."""
    relevant = set(ordinals)
    rest = [ordinal for ordinal in range(frame.num_rows)
            if ordinal not in relevant][:OTHER_ROWS_SHOWN]
    return [_build_preview_row(frame, ordinal, ordinal in relevant)
            for ordinal in [*ordinals, *rest]]


def _build_preview_row(frame: pa.Table, ordinal: RowOrdinal,
                       relevant: bool) -> PreviewRow:
    cells = [frame.column(name)[ordinal].as_py() for name in frame.column_names]
    stamped = (frame.column(SOURCE_ROW_COLUMN)[ordinal].as_py()
               if SOURCE_ROW_COLUMN in frame.column_names else None)
    return PreviewRow(label=f"{stamped if stamped is not None else ordinal + 1:,}",
                      relevant=relevant, cells=cells)


def _read_the_binding(manifest: RunManifest, stage_id: StageId) -> dict[str, Any]:
    # Two shapes in the wild: a `files` list, and the fields flat on the binding.
    binding = manifest.input_bindings.get(stage_id) or {}
    return (binding.get("files") or [binding])[0]


def _read_the_cap(manifest: RunManifest, stage_id: StageId) -> int | None:
    cap = manifest.parameters.limits.get(stage_id)
    return int(cap) if cap else None


def _read_the_row_count_before_the_cut(manifest: RunManifest, stage_id: StageId,
                                       read: int) -> int | None:
    """Only the cut note carries it, so an unrecognised note means no number at all."""
    for record in manifest.stage_records:
        if record.stage_id != stage_id:
            continue
        for note in record.notes or []:
            before = _read_the_count_the_note_states(note)
            if before is not None and before >= read:
                return before
    return None


def _read_the_count_the_note_states(note: str) -> int | None:
    words = note.replace(",", "").split()
    if not words or not words[0].startswith(("limit=", "offset=")):
        return None
    counted = [int(word) for word in words if word.isdigit()]
    return max(counted) if counted else None


def _read_the_cited_cell(outputs: Path,
                         citation: StageOutputCellCitation) -> JsonScalar:
    frame = read_frame_table(outputs / f"{citation.stage_id}.parquet")
    return frame.column(citation.column)[citation.row_ordinal].as_py()
