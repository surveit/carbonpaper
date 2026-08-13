"""Turn an eval's `TableRef` into rows — the one module that resolves one at all, and so
the only reader of its `sha256` (`tests/arch/test_eval_dataset_read_is_owned.py`).
Consumers: the runner (which injects it) and the run page (which shows it beside the scores).
"""
from __future__ import annotations

import pandas as pd

from app.core.errors import EvalNotScorableError
from app.core.frames import read_frame_file, read_source_csv, read_source_json_lines
from app.models import TableRef
from app.models.stages.input_data import FileFormat
from app.services.uploads import open_stored_file


def read_table_ref(table: TableRef) -> pd.DataFrame:
    _, path = open_stored_file(table.sha256)
    if table.format == FileFormat.csv:
        return read_source_csv(path)
    if table.format == FileFormat.parquet:
        return read_frame_file(path)
    if table.format == FileFormat.json:
        return read_source_json_lines(path)
    raise EvalNotScorableError(f"unsupported eval dataset format: {table.format}")


def read_dataset_filename(table: TableRef) -> str:
    """What a reader is shown for this dataset: the stored file's name, not a copy in the ref."""
    return open_stored_file(table.sha256)[0].filename
