"""Profile columns of a stage's stored output or of a stored source file, so a declared
schema comes from the data rather than from the methodology's prose."""
from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import NamedTuple

import pandas as pd
import pyarrow as pa

from app.core.source_files import (
    FileFormat, SheetSurvey, read_source_file, resolve_file_format, survey_xlsx_sheets,
)
from app.core.column_profile import (
    ColumnProfile, NumericRange, StageOutputProfile, TableProfile, ValueCount,
)
from app.core.file_shape import (
    VALUES_KEPT, FileShape, StoredFileShape, measure_column_shape,
)
from app.core.ids import ID
from app.core.frames import frame_to_table, table_to_frame
from app.services.run import read_stage_output
from app.core.files import open_project_file

# What the profile read pins per format. A source file is read so that NOTHING is
# coerced — `str` keeps a zero-padded "002" out of the integer 2, `False` is pandas'
# infer-nothing for json — because which column is really a number is the decision
# the caller makes off this profile, and a read that has already decided hides it.
# parquet and geojson carry real types, so there is nothing to pin.
_PROFILE_DTYPE: dict[FileFormat, type | bool | None] = {
    FileFormat.csv: str,
    FileFormat.tsv: str,
    FileFormat.xlsx: str,
    FileFormat.json: False,
    FileFormat.parquet: None,
    FileFormat.geojson: None,
}


class StoredFileFrame(NamedTuple):
    filename: str
    format: FileFormat
    frame: pd.DataFrame


def profile_stage_output(
    project_id: str, run_id: str, stage_id: str, columns: list[str], *, max_values: int,
) -> StageOutputProfile:
    profile = profile_table(
        frame_to_table(read_stage_output(project_id, run_id, stage_id)),
        columns, max_values=max_values)
    return StageOutputProfile(
        run_id=run_id, stage_id=stage_id,
        row_count=profile.row_count, columns=profile.columns,
    )


def survey_stored_workbook(
    project_id: str, file_id: str, *, from_row: int = 0,
) -> list[SheetSurvey]:
    """Refuses a non-xlsx rather than surveying it: no other format has sheets."""
    record, path = open_project_file(project_id, file_id)
    fmt = resolve_file_format(record.filename)
    if fmt != FileFormat.xlsx:
        raise ValueError(
            f"'{record.filename}' is a {fmt.value} file, which has one table and no "
            "sheets — profile_file reads it directly")
    return survey_xlsx_sheets(path, from_row=from_row)


def profile_stored_file(
    project_id: str, file_id: str, columns: list[str] | None, *, max_values: int,
    sheet_name: str | int = 0, header_row: int = 0, first_column: int = 0,
) -> TableProfile:
    stored = read_stored_file_frame(
        project_id, file_id, sheet_name=sheet_name, header_row=header_row,
        first_column=first_column,
    )
    return profile_table(frame_to_table(stored.frame), columns, max_values=max_values)


def read_stored_file_frame(
    project_id: str, file_id: str, *, sheet_name: str | int = 0,
    header_row: int = 0, first_column: int = 0,
) -> StoredFileFrame:
    record, path = open_project_file(project_id, file_id)
    file_format = resolve_file_format(record.filename)
    frame = read_source_file(
        path, file_format, dtype=_PROFILE_DTYPE[file_format], sheet_name=sheet_name,
        header_row=header_row, first_column=first_column,
    )
    return StoredFileFrame(filename=record.filename, format=file_format, frame=frame)


def read_file_shape(project_id: ID, file_id: ID) -> FileShape:
    """Measured on the first ask and kept: a stored file's bytes never change."""
    stored = StoredFileShape.find(file_id=file_id)
    if stored:
        return stored[0].shape
    shape = measure_file_shape(project_id, file_id, max_values=VALUES_KEPT)
    StoredFileShape(file_id=file_id, shape=shape).save()
    return shape


def measure_file_shape(project_id: str, file_id: str, *, max_values: int) -> FileShape:
    """Every column of a stored file, as the fill and values a reader checks it against."""
    frame = read_stored_file_frame(project_id, file_id).frame
    columns = []
    for name in frame.columns:
        present = frame[name].dropna()
        columns.append(measure_column_shape(
            str(name), [str(value) for value in present],
            null_count=len(frame) - len(present), max_values=max_values))
    return FileShape(row_count=len(frame), columns=columns)


def measure_stage_rows_shape(
    project_id: str, run_id: str, stage_id: str, *, ordinals: Sequence[int],
    max_values: int, never_numbers: Collection[str] = (),
) -> FileShape:
    """`never_numbers` are the columns a pinned schema declares `str`. Named rows only."""
    frame = read_stage_output(project_id, run_id, stage_id)
    behind = frame.iloc[[o for o in ordinals if 0 <= o < len(frame)]]
    columns = []
    for name in behind.columns:
        present = behind[name].dropna()
        columns.append(measure_column_shape(
            str(name), [str(value) for value in present],
            null_count=len(behind) - len(present), max_values=max_values,
            may_read_as_number=str(name) not in never_numbers))
    return FileShape(row_count=len(behind), columns=columns)


def profile_table(
    table: pa.Table, columns: list[str] | None, *, max_values: int
) -> TableProfile:
    """Every miss raises, naming what exists — never an empty or partial profile."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    frame = table_to_frame(table)
    return TableProfile(
        row_count=len(frame),
        columns=[_profile_column(frame, name, max_values)
                 for name in (columns if columns is not None else list(frame.columns))],
    )


def _profile_column(frame: pd.DataFrame, name: str, max_values: int) -> ColumnProfile:
    if name not in frame.columns:
        raise ValueError(
            f"the output holds no column '{name}' — its columns: "
            + (", ".join(str(column) for column in frame.columns) or "(none)")
        )
    present = frame[name].dropna()
    counts = _count_distinct_values(present)
    return ColumnProfile(
        column=name,
        null_count=len(frame) - len(present),
        distinct_count=len(counts),
        values=counts[:max_values],
        truncated=len(counts) > max_values,
        value_range=_summarize_numeric_range(_read_text_as_numbers(present)),
    )


def _read_text_as_numbers(present: pd.Series) -> pd.Series:
    """All or nothing: one value that is not a number means the column is not one."""
    if present.empty or pd.api.types.is_numeric_dtype(present):
        return present
    converted = pd.to_numeric(present, errors="coerce")
    return present if converted.isna().any() else converted


def _count_distinct_values(present: pd.Series) -> list[ValueCount]:
    """Commonest first, in text form — which keeps a list/dict cell countable."""
    as_text = present.map(lambda value: value if isinstance(value, str) else str(value))
    counted = [
        ValueCount(value=str(value), count=int(count))
        for value, count in as_text.value_counts().items()
    ]
    return sorted(counted, key=lambda seen: (-seen.count, seen.value))


def _summarize_numeric_range(present: pd.Series) -> NumericRange | None:
    if present.empty or not pd.api.types.is_numeric_dtype(present):
        return None
    if pd.api.types.is_bool_dtype(present):
        return None
    return NumericRange(
        min=float(present.min()),
        max=float(present.max()),
        mean=float(present.mean()),
        median=float(present.median()),
    )
