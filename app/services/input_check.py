"""Whether a slice of an input stage's frame is still a rectangle of the file it read."""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

import pyarrow as pa
from pydantic import BaseModel

from app.core.errors import StageNotInRun
from app.core.frames import frame_to_table, read_frame_table
from app.core.source_files import read_source_file, resolve_file_format
from app.models.branch_analysis import RowOrdinal
from app.models.schema import StageId
from app.models.stages.input_data import FileConnectorParams
from app.services import run as run_service
from app.services.versioning import load_version_stages
from app.services.workspace import resolve_run_dir

# The 1-based sheet line a file loader stamps on each row; absent, rows align by position.
SOURCE_ROW_COLUMN = "source_row"


class SliceComparison(BaseModel):
    """`mismatches` counts cells, not rows: one wrong cell is one mismatch."""

    rows: int
    columns: int
    cells: int
    mismatches: int
    filename: str
    located_by: str


def compare_slice_to_the_file(project_id: str, run_id: str, stage_id: StageId,
                              ordinals: Sequence[RowOrdinal],
                              columns: Sequence[str]) -> SliceComparison:
    """Re-reads the file the way the run read it, then compares the slice cell by cell."""
    path = _read_the_binding(project_id, run_id, stage_id)
    ran = read_frame_table(
        resolve_run_dir(project_id, run_id) / "outputs" / f"{stage_id}.parquet")
    source = frame_to_table(_reread_the_source(project_id, run_id, stage_id, path))
    ran_rows = ran.take(pa.array(list(ordinals)))
    source_rows, located_by = _align_rows(
        ran_rows, source, _skipped_rows(project_id, run_id, stage_id), ordinals)
    mismatches = sum(_count_differing_cells(ran_rows, source_rows, column)
                     for column in columns)
    return SliceComparison(rows=len(ordinals), columns=len(columns),
                           cells=len(ordinals) * len(columns), mismatches=mismatches,
                           filename=path.name, located_by=located_by)


def _reread_the_source(project_id: str, run_id: str, stage_id: StageId, path: Path):
    params = _read_the_connector_params(project_id, run_id, stage_id)
    return read_source_file(
        path, params.format or resolve_file_format(str(path)), dtype=str,
        sheet_name=params.sheet_name, header_row=params.header_row,
        first_column=params.first_column,
        source_row_column=params.source_row_column)


def _align_rows(ran_rows: pa.Table, source: pa.Table, skipped: int,
                ordinals: Sequence[RowOrdinal]) -> tuple[pa.Table, str]:
    """The file's rows in the slice's order, by stamped line where there is one."""
    if SOURCE_ROW_COLUMN in ran_rows.column_names \
            and SOURCE_ROW_COLUMN in source.column_names:
        at_line = {int(line): position for position, line
                   in enumerate(source.column(SOURCE_ROW_COLUMN).to_pylist())}
        wanted = [at_line[int(line)]
                  for line in ran_rows.column(SOURCE_ROW_COLUMN).to_pylist()]
        return source.take(pa.array(wanted)), SOURCE_ROW_COLUMN
    return (source.take(pa.array([skipped + ordinal for ordinal in ordinals])),
            "row position")


def _count_differing_cells(ran_rows: pa.Table, source_rows: pa.Table,
                           column: str) -> int:
    ran = _as_text(ran_rows.column(column).to_pylist())
    reread = _as_text(source_rows.column(column).to_pylist())
    return sum(1 for here, there in zip(ran, reread) if here != there)


def _as_text(cells: list) -> list[str | None]:
    return [None if cell is None else str(cell) for cell in cells]


def _read_the_binding(project_id: str, run_id: str, stage_id: StageId) -> Path:
    binding = run_service.read_run_status(project_id, run_id)["input_bindings"].get(stage_id)
    if not binding:
        raise StageNotInRun(f"this run recorded no file for '{stage_id}'")
    files = binding.get("files") or [binding]
    return Path(files[0]["path"])


def _read_the_connector_params(project_id: str, run_id: str,
                               stage_id: StageId) -> FileConnectorParams:
    version = run_service.read_pinned_version(project_id, run_id)
    for authored in load_version_stages(project_id, version):
        connector = getattr(authored, "connector", None)
        if authored.id == stage_id and connector is not None:
            return connector.params
    raise StageNotInRun(f"the pinned version has no file stage '{stage_id}'")


def _skipped_rows(project_id: str, run_id: str, stage_id: StageId) -> int:
    offsets = run_service.read_run_status(project_id, run_id)["parameters"]["offsets"]
    return int(offsets.get(stage_id) or 0)
