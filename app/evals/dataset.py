"""Read an eval's dataset file — the one place a `TableRef`'s format picks a reader,
and the one place its path is joined to a root. That root is the project's own
directory, which this module resolves from the project id: a caller supplies no root
and so cannot supply a wrong one. Consumers: the runner and the run page.
"""
from __future__ import annotations

import pandas as pd

from app.core.errors import EvalNotScorableError
from app.core.frames import read_frame_file, read_source_csv, read_source_json_lines
from app.models import TableRef
from app.models.stages.input_data import FileFormat
from app.services.workspace import resolve_project_dir


def read_table_ref(project_id: str, table: TableRef) -> pd.DataFrame:
    path = resolve_project_dir(project_id) / table.path
    if not path.is_file():
        raise FileNotFoundError(
            f"project '{project_id}' has no eval dataset at '{table.path}' "
            f"(looked in {path.parent})")
    if table.format == FileFormat.csv:
        return read_source_csv(path)
    if table.format == FileFormat.parquet:
        return read_frame_file(path)
    if table.format == FileFormat.json:
        return read_source_json_lines(path)
    raise EvalNotScorableError(f"unsupported eval dataset format: {table.format}")
