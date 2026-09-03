"""Read an eval's dataset file — the one place a `TableRef`'s format picks a reader.
Consumers: the runner (which injects it) and the run page (which shows it beside the scores).
"""
from __future__ import annotations

import pandas as pd

from app.core.errors import EvalNotScorableError
from app.core.frames import read_frame_file, read_source_csv, read_source_json_lines
from app.core.paths import repo_root
from app.core.source_files import text_on_disk_columns
from app.models import TableRef
from app.models.stages.input_data import FileFormat


# `table.path` is checkout-relative — an eval dataset is a file in the repository,
# not project storage, so it is the repo root it hangs off.
def read_table_ref(table: TableRef) -> pd.DataFrame:
    path = repo_root() / table.path
    if table.format == FileFormat.csv:
        return read_source_csv(path, dtype=_declared_dtype(table))
    if table.format == FileFormat.tsv:
        return read_source_csv(path, delimiter="\t", dtype=_declared_dtype(table))
    if table.format == FileFormat.parquet:
        return read_frame_file(path)
    if table.format == FileFormat.json:
        return read_source_json_lines(path)
    raise EvalNotScorableError(f"unsupported eval dataset format: {table.format}")


def _declared_dtype(table: TableRef) -> dict[str, type[str]] | None:
    """The same text-on-disk pin an input_data stage reads its own file under."""
    column_types = {c.name: c.type for c in table.table_schema.columns}
    pinned = {name: str for name in text_on_disk_columns(column_types, table.format)}
    return pinned or None
