"""Table references — a pointer to tabular data on disk plus its declared shape.
Kept out of the eval contract so nothing has to import evals to name a table.
"""
from __future__ import annotations

from app.models.schema import TableSchema, _Base
from app.models.stages.input_data import FileFormat


class TableRef(_Base):
    """A tabular file (CSV/parquet/…) with its schema. The schema is required —
    a table you can't validate against is not a table we'll load blindly."""
    path: str
    format: FileFormat = FileFormat.csv
    table_schema: TableSchema
