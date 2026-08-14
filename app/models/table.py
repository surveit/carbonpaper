"""Table references — a pointer to a stored file plus its declared shape.
Kept out of the eval contract so nothing has to import evals to name a table.
"""
from __future__ import annotations

from app.models.schema import TableSchema, _Base
from app.models.stages.input_data import FileFormat


class TableRef(_Base):
    """`sha256` names a file the workspace stores, which is the only place tabular data lives."""

    sha256: str
    format: FileFormat = FileFormat.csv
    table_schema: TableSchema
