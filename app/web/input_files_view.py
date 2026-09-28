"""What the Input files tab shows: each file a figure read, sliced to what it needed."""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Sequence

import pyarrow as pa
from pydantic import BaseModel

from app.core.errors import MissingInputBindingError
from app.core.files import ProjectFile, ProjectFileIndex, index_project_files
from app.core.figure_text import render_figure
from app.core.file_shape import VALUES_KEPT, measure_column_shape
from app.core.frames import read_frame_table, read_native_cell_as_json
from app.core.json_types import JsonScalar
from app.models.branch_analysis import RowOrdinal
from app.models.citations import StageOutputCellCitation
from app.models.schema import StageId
from app.models.stages.input_data import FileConnectorParams, InputDataStage
from app.models.records.run_manifest import RunManifest
from app.runtime.errors import MissingLineage
from app.runtime.lineage import RowLineage, RowParent
from app.runtime.manifest import read_run_manifest
from app.services import run as run_service
from app.services.scope import find_rows_reached_per_stage, read_run_branches
from app.services.workspace import resolve_run_dir
from app.web.file_detail_view import ColumnRow, build_column_row
from app.web.file_sizes import describe_bytes
from app.web.column_walk import ColumnAt, find_columns_behind
from app.models.run_manifest import InputBinding, RunKind, read_input_bindings

# Rows shown beside the relevant ones when a reader widens the preview to the file.
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
    # None where no file this project holds hashes to the bytes the run read.
    file_id: str | None
    # Both None for an upload, and unless the run named this file by its id.
    origin_url: str | None
    fetched_at: str | None
    # None where nothing the run wrote says how many rows the file holds.
    rows_in_file: int | None
    cap: int | None
    columns_relevant: list[str]
    columns_read: list[str]
    shape_over_relevant_rows: list[ColumnRow]
    shape_over_every_row: list[ColumnRow]
    row_label: str
    rows: list[PreviewRow]
    # Rows of the stage's frame, which holds each file the stage read, one after another.
    ordinals_relevant: list[RowOrdinal]
    ordinals_read: list[RowOrdinal]

    @property
    def rows_relevant(self) -> int:
        return len(self.ordinals_relevant)

    @property
    def rows_read(self) -> int:
        return len(self.ordinals_read)

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
    manifest = read_run_manifest(project_id, run_id, RunKind.production)
    # Bound as the run bound it: which column holds the stamped row is a connector param.
    workflow = run_service.load_run_workflow(project_id, manifest.to_dict())
    behind = find_columns_behind(workflow.index_workflow_stages_by_id(), set(reached),
                                 ColumnAt(citation.stage_id, citation.column))
    outputs = resolve_run_dir(project_id, run_id, RunKind.production) / "outputs"
    stored_files = index_project_files(project_id)
    reading = {placed.id: placed.stage for placed in workflow.list_workflow_stages()
               if isinstance(placed.stage, InputDataStage) and placed.id in reached}
    files = [one_file
             # The run's order, not the workflow's: the reader met these files in it.
             for stage_id in _in_the_order_the_run_read_them(manifest, list(reading))
             for one_file in _build_each_file(
                 outputs, manifest, stored_files, reading[stage_id],
                 branches.lineages.get(stage_id), sorted(reached[stage_id]),
                 sorted(behind.get(stage_id, ())))]
    return InputFilesView(citation=citation, files=files,
                          value=_read_the_cited_cell(outputs, citation))


def _in_the_order_the_run_read_them(manifest: RunManifest,
                                   stage_ids: Sequence[StageId]) -> list[StageId]:
    ran = [record.stage_id for record in manifest.stage_records]
    return sorted(stage_ids, key=lambda stage_id: (ran.index(stage_id)
                                                   if stage_id in ran else len(ran)))


def _build_each_file(outputs: Path, manifest: RunManifest, stored_files: ProjectFileIndex,
                     stage: InputDataStage, lineage: RowLineage | None,
                     reached: Sequence[RowOrdinal],
                     relevant: Sequence[str]) -> list[InputFileSlice]:
    """A file none of the figure's rows came from was read but not needed, so it is left out."""
    frame = read_frame_table(outputs / f"{stage.id}.parquet")
    origins = _read_where_each_row_came_from(stage.id, lineage, frame.num_rows)
    return [_build_one_file(frame, manifest, stage, binding,
                            stored_files.find(binding.file_id, binding.sha256),
                            origin_by_ordinal,
                            reached, relevant)
            for binding, origin_by_ordinal in _split_the_rows_by_file(manifest, stage.id,
                                                                     origins)
            if not origin_by_ordinal.keys().isdisjoint(reached)]


def _build_one_file(frame: pa.Table, manifest: RunManifest, stage: InputDataStage,
                    binding: InputBinding, stored: ProjectFile | None,
                    origin_by_ordinal: dict[RowOrdinal, RowParent],
                    reached: Sequence[RowOrdinal], relevant: Sequence[str]) -> InputFileSlice:
    ordinals = [ordinal for ordinal in reached if ordinal in origin_by_ordinal]
    stamped = _find_the_stamped_row_column(stage, frame)
    # A byte match may be a later send of the same bytes, fetched from somewhere else.
    recorded = stored if stored is not None and stored.id == binding.file_id else None
    # The cut note counts every file the stage read, so it is this file's count only alone.
    counted = (_read_the_row_count_before_the_cut(manifest, stage.id, frame.num_rows)
               if len(_list_the_files_read(manifest, stage.id)) == 1 else None)
    return InputFileSlice(
        stage_id=stage.id,
        filename=binding.filename,
        size_label=describe_bytes(binding.bytes) if binding.bytes is not None else "",
        file_id=None if stored is None else stored.id,
        origin_url=None if recorded is None else recorded.origin_url,
        fetched_at=None if recorded is None else recorded.fetched_at,
        rows_in_file=counted,
        cap=_read_the_cap(manifest, stage.id),
        columns_relevant=list(relevant),
        columns_read=[str(name) for name in frame.column_names],
        shape_over_relevant_rows=_measure_shape(frame.take(pa.array(ordinals))),
        shape_over_every_row=_measure_shape(frame.take(pa.array(list(origin_by_ordinal)))),
        row_label=("row" if stamped is None else "sheet row"),
        rows=_build_preview(frame, ordinals, origin_by_ordinal, stamped),
        ordinals_relevant=ordinals,
        ordinals_read=list(origin_by_ordinal),
    )


def _read_where_each_row_came_from(stage_id: StageId, lineage: RowLineage | None,
                                   rows: int) -> list[RowParent]:
    """An input's lineage names each row's file, and counts the row within that file."""
    origins = [] if lineage is None else [
        parent for parents in lineage.parents for parent in parents if parent.source_file]
    if len(origins) != rows:
        raise MissingLineage(
            f"'{stage_id}' recorded the file of {len(origins)} of its {rows} rows")
    return origins


def _split_the_rows_by_file(manifest: RunManifest, stage_id: StageId,
                            origins: Sequence[RowParent]
                            ) -> list[tuple[InputBinding, dict[RowOrdinal, RowParent]]]:
    per_file: dict[tuple[str | None, str | None], dict[RowOrdinal, RowParent]] = {}
    for ordinal, origin in enumerate(origins):
        per_file.setdefault((origin.source_file, origin.source_file_sha), {})[ordinal] = origin
    files_read = _list_the_files_read(manifest, stage_id)
    return [(_find_the_binding(stage_id, files_read, path, sha256), origin_by_ordinal)
            for (path, sha256), origin_by_ordinal in per_file.items()]


def _list_the_files_read(manifest: RunManifest, stage_id: StageId) -> list[InputBinding]:
    return [binding for binding in read_input_bindings(manifest.to_dict())
            if binding.stage_id == stage_id]


def _find_the_binding(stage_id: StageId, files_read: Sequence[InputBinding],
                      path: str | None, sha256: str | None) -> InputBinding:
    for binding in files_read:
        if (binding.path, binding.sha256) == (path, sha256):
            return binding
    raise MissingInputBindingError(
        f"'{stage_id}' holds rows read from {path} (sha256 {sha256}), "
        "a file this run records no binding for")


def _find_the_stamped_row_column(stage: InputDataStage, frame: pa.Table) -> str | None:
    params = stage.connector.params
    if not isinstance(params, FileConnectorParams):
        return None
    column = params.source_row_column
    # Only an xlsx read stamps one, and a stage whose schema omits the column drops it.
    return column if column in frame.column_names else None


def _measure_shape(frame: pa.Table) -> list[ColumnRow]:
    rows = max(frame.num_rows, 1)
    return [build_column_row(_measure_one_column(frame, str(name)), rows)
            for name in frame.column_names]


def _measure_one_column(frame: pa.Table, column: str):
    cells = frame.column(column).to_pylist()
    filled = [str(cell) for cell in cells if cell is not None]
    return measure_column_shape(column, filled, null_count=len(cells) - len(filled),
                                max_values=VALUES_KEPT)


def _build_preview(frame: pa.Table, ordinals: Sequence[RowOrdinal],
                   origin_by_ordinal: dict[RowOrdinal, RowParent],
                   stamped: str | None) -> list[PreviewRow]:
    """The relevant rows, then the head of the rest for a reader who widens the view."""
    relevant = set(ordinals)
    rest = [ordinal for ordinal in origin_by_ordinal
            if ordinal not in relevant][:OTHER_ROWS_SHOWN]
    return [_build_preview_row(
                frame, ordinal, ordinal in relevant,
                _read_the_row_number(frame, stamped, ordinal, origin_by_ordinal[ordinal]))
            for ordinal in [*ordinals, *rest]]


def _build_preview_row(frame: pa.Table, ordinal: RowOrdinal, relevant: bool,
                       number: JsonScalar) -> PreviewRow:
    cells = [read_native_cell_as_json(frame, name, ordinal) for name in frame.column_names]
    return PreviewRow(label=render_figure(number), relevant=relevant, cells=cells)


def _read_the_row_number(frame: pa.Table, stamped: str | None, ordinal: RowOrdinal,
                         origin: RowParent) -> JsonScalar:
    if stamped is None:
        return origin.row_ordinal + 1
    return read_native_cell_as_json(frame, stamped, ordinal)


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
    return read_native_cell_as_json(frame, citation.column, citation.row_ordinal)
