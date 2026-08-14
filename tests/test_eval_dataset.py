"""An eval dataset is a stored file — the same store a run's inputs come from, reached
by sha256 rather than by a path into the checkout nobody can write to on a server."""
from __future__ import annotations

import io

import pandas as pd
import pytest

from app.core.errors import EvalNotScorableError
from app.core.frames import write_frame_file
from app.evals.dataset import read_dataset_filename, read_table_ref
from app.models import TableRef
from app.models.schema import TableSchema
from app.models.stages.input_data import FileFormat
from app.services.errors import FileNotStoredError
from app.services.uploads import save_upload

_FRAME = pd.DataFrame({"k": ["a", "b"], "v": [1, 2]})
_SCHEMA = TableSchema.model_validate({"columns": [
    {"name": "k", "type": "str", "nullable": True},
    {"name": "v", "type": "int", "nullable": True}]})


def _store(name: str, body: bytes, project_id: str = "demo") -> str:
    return save_upload(name, io.BytesIO(body), project_id).sha256


def test_a_csv_dataset_reads_back_through_the_file_store():
    sha256 = _store("cases.csv", _FRAME.to_csv(index=False).encode())

    frame = read_table_ref(TableRef(sha256=sha256, format=FileFormat.csv,
                                    table_schema=_SCHEMA))

    assert list(frame["k"]) == ["a", "b"]


def test_a_parquet_dataset_reads_back_through_the_file_store(tmp_path):
    source = tmp_path / "cases.parquet"
    write_frame_file(_FRAME, source)
    sha256 = _store("cases.parquet", source.read_bytes())

    frame = read_table_ref(TableRef(sha256=sha256, format=FileFormat.parquet,
                                    table_schema=_SCHEMA))

    assert list(frame["v"]) == [1, 2]


def test_a_dataset_in_another_project_is_still_readable_by_address():
    """A TableRef names bytes, not a project — the config it sits in is what has one."""
    sha256 = _store("cases.csv", _FRAME.to_csv(index=False).encode(), project_id="other")

    frame = read_table_ref(TableRef(sha256=sha256, format=FileFormat.csv,
                                    table_schema=_SCHEMA))

    assert len(frame) == 2


def test_the_name_shown_for_a_dataset_is_the_stored_files_own():
    """The ref carries no filename, so a rename cannot leave the page naming the wrong file."""
    sha256 = _store("hard_cases.csv", _FRAME.to_csv(index=False).encode())

    ref = TableRef(sha256=sha256, format=FileFormat.csv, table_schema=_SCHEMA)

    assert read_dataset_filename(ref) == "hard_cases.csv"


def test_a_sha256_the_store_does_not_hold_is_loud():
    ref = TableRef(sha256="0" * 64, format=FileFormat.csv, table_schema=_SCHEMA)

    with pytest.raises(FileNotStoredError, match="anywhere in the store"):
        read_table_ref(ref)


def test_a_format_no_reader_handles_is_refused_rather_than_guessed():
    sha256 = _store("cases.xlsx", b"not really a workbook")
    ref = TableRef(sha256=sha256, format=FileFormat.xlsx, table_schema=_SCHEMA)

    with pytest.raises(EvalNotScorableError, match="unsupported eval dataset format"):
        read_table_ref(ref)
