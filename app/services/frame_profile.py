"""Profile columns of a stage's stored output or of a stored source file, so a declared
schema comes from the data rather than from the methodology's prose."""
from __future__ import annotations


from app.core.source_files import (
    FileFormat, SheetSurvey, read_source_file, resolve_file_format, survey_xlsx_sheets,
)
from app.core.column_profile import StageOutputProfile, TableProfile, profile_frame
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
    profile = profile_frame(
        read_stage_output(project, run_id, stage_id), columns, max_values=max_values)
    return StageOutputProfile(
        run_id=run_id, stage_id=stage_id,
        row_count=profile.row_count, columns=profile.columns,
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
    _, path = open_project_file(project, sha256)
    fmt = resolve_file_format(str(path))
    frame = read_source_file(
        path, fmt, dtype=_PROFILE_DTYPE[fmt], sheet_name=sheet_name,
        header_row=header_row, first_column=first_column,
    )
    return profile_frame(frame, columns, max_values=max_values)
