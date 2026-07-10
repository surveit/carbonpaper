"""Read a tabular file and check it against a declared TableSchema.

Thin seam over pandas + validate_dataframe so the eval form validates actual
bytes, not just declared shapes. `read_table` is also the reader
`app.runtime.stages.input_data` calls for its file connector, so an
eval-dataset file and a workflow's own input data are read one way. Fails
loudly: a missing or unreadable file is an exception, never an empty frame."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models import FileFormat, TableSchema
from app.runtime.validation import ValidationReport, validate_dataframe


def read_table(path: Path, file_format: FileFormat) -> pd.DataFrame:
    if not path.is_file():
        raise FileNotFoundError(f"table file not found: {path}")
    if file_format == FileFormat.csv:
        try:
            return pd.read_csv(path)
        except pd.errors.EmptyDataError as err:
            raise ValueError(f"table file is empty: {path}") from err
    if file_format == FileFormat.parquet:
        return pd.read_parquet(path)
    if file_format == FileFormat.json:
        return pd.read_json(path, lines=True)
    raise ValueError(f"unsupported eval table format: {file_format} ({path})")


def table_columns(path: Path, file_format: FileFormat) -> list[str]:
    return [str(c) for c in read_table(path, file_format).columns]


def validate_table_file(path: Path, file_format: FileFormat,
                        schema: TableSchema) -> ValidationReport:
    df = read_table(path, file_format)
    return validate_dataframe(df, schema, stage_id=path.name, phase="input")
