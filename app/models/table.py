"""Table references — a pointer to tabular data on disk plus its declared shape.

This is a general concept, not an eval one: a compiler or the DAG author can mint
a `TableRef` when wiring up input data, and evals reuse it to point at fixtures
and expected-output tables. Kept in its own module so nothing has to import the
eval contract to name a table.
"""
from __future__ import annotations

from app.models.schema import TableSchema, _Base
from app.models.stage import FileFormat


class TableRef(_Base):
    """A tabular file (CSV/parquet/…) with its schema. The schema is required —
    a table you can't validate against is not a table we'll load blindly."""
    path: str
    format: FileFormat = FileFormat.csv
    table_schema: TableSchema
