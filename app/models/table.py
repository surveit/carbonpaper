"""Table references — a pointer to tabular data on disk plus its declared shape.
Kept out of the eval contract so nothing has to import evals to name a table.
"""
from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import field_validator

from app.models.schema import TableSchema, _Base
from app.models.stages.input_data import FileFormat


class TableRef(_Base):
    # Relative to the project directory, and rejected below if it could leave it.
    path: str
    format: FileFormat = FileFormat.csv
    table_schema: TableSchema

    @field_validator("path")
    @classmethod
    def _stays_inside_the_project(cls, v: str) -> str:
        parts = PurePosixPath(v).parts
        if PurePosixPath(v).is_absolute() or ".." in parts or not parts:
            raise ValueError(
                f"a table path is relative to its project directory and may not leave it: {v!r}")
        return v
