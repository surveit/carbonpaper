"""Profile columns of a stage's stored output or of a stored source file, so a declared
schema comes from the data rather than from the methodology's prose."""
from __future__ import annotations

import pandas as pd

from app.core.source_files import (
    FileFormat, SheetSurvey, read_source_file, resolve_file_format, survey_xlsx_sheets,
)
from app.models.column_profile import (
    ColumnProfile, NumericRange, StageOutputProfile, TableProfile, ValueCount,
)
from app.services.run import read_stage_output
from app.services.uploads import open_project_file

# What the profile read pins per format. A source file is read so that NOTHING is
# coerced — `str` keeps a zero-padded "002" out of the integer 2, `False` is pandas'
# infer-nothing for json — because which column is really a number is the decision
# the caller makes off this profile, and a read that has already decided hides it.
# parquet and geojson carry real types, so there is nothing to pin.
_PROFILE_DTYPE: dict[FileFormat, type | bool | None] = {
    FileFormat.csv: str,
    FileFormat.xlsx: str,
    FileFormat.json: False,
    FileFormat.parquet: None,
    FileFormat.geojson: None,
}


def profile_stage_output(
    project: str, run_id: str, stage_id: str, columns: list[str], *, max_values: int,
) -> StageOutputProfile:
    """Every miss raises, naming what exists — never an empty or partial profile."""
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    frame = read_stage_output(project, run_id, stage_id)
    return StageOutputProfile(
        run_id=run_id,
        stage_id=stage_id,
        row_count=len(frame),
        columns=[_profile_column(frame, name, max_values) for name in columns],
    )


def survey_stored_workbook(
    project: str, sha256: str, *, from_row: int = 0,
) -> list[SheetSurvey]:
    """Refuses a non-xlsx rather than surveying it: no other format has sheets."""
    record, path = open_project_file(project, sha256)
    fmt = resolve_file_format(str(path))
    if fmt != FileFormat.xlsx:
        raise ValueError(
            f"'{record.filename}' is a {fmt.value} file, which has one table and no "
            "sheets — profile_file reads it directly")
    return survey_xlsx_sheets(path, from_row=from_row)


def profile_stored_file(
    project: str, sha256: str, columns: list[str] | None, *, max_values: int,
    sheet_name: str | int = 0, header_row: int = 0, first_column: int = 0,
) -> TableProfile:
    if max_values < 1:
        raise ValueError(f"max_values must be at least 1, got {max_values}")
    _, path = open_project_file(project, sha256)
    fmt = resolve_file_format(str(path))
    frame = read_source_file(
        path, fmt, dtype=_PROFILE_DTYPE[fmt], sheet_name=sheet_name,
        header_row=header_row, first_column=first_column,
    )
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
