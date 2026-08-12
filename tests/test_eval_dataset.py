"""Tests for app/evals/dataset.py — where a TableRef's path meets a root. The root is
the project directory in the configured workspace, never the checkout: a dataset the
app itself wrote (save_dataset_upload) must be the one the runner reads back."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import EvalNotScorableError
from app.evals.dataset import read_table_ref
from app.evals.store import save_dataset_upload
from app.models import Column, TableRef, TableSchema
from app.models.stages.input_data import FileFormat

_ROWS = b"doc_id,score\na,1\nb,-1\n"
_SCHEMA = TableSchema(columns=[Column(name="doc_id", type="str", nullable=True),
                               Column(name="score", type="int", nullable=True)])


def _uploaded(projects_root: Path) -> TableRef:
    project_dir = projects_root / "demo"
    project_dir.mkdir()
    return TableRef(path=save_dataset_upload(project_dir, "cases.csv", _ROWS),
                    format=FileFormat.csv, table_schema=_SCHEMA)


def test_a_dataset_the_app_uploaded_reads_back(projects_root):
    frame = read_table_ref("demo", _uploaded(projects_root))

    assert list(frame["doc_id"]) == ["a", "b"]


def test_a_missing_dataset_names_the_project_and_where_it_looked(projects_root):
    (projects_root / "demo").mkdir()
    table = TableRef(path="eval_data/absent.csv", format=FileFormat.csv, table_schema=_SCHEMA)

    with pytest.raises(FileNotFoundError) as caught:
        read_table_ref("demo", table)

    assert "demo" in str(caught.value) and "eval_data/absent.csv" in str(caught.value)


@pytest.mark.parametrize("path", ["/etc/passwd", "../elsewhere/cases.csv", ""])
def test_a_path_that_could_leave_the_project_is_refused_at_the_model(path):
    with pytest.raises(ValueError, match="may not leave it"):
        TableRef(path=path, format=FileFormat.csv, table_schema=_SCHEMA)


def test_an_unreadable_format_is_refused_rather_than_guessed(projects_root):
    table = _uploaded(projects_root).model_copy(update={"format": "xlsx"})

    with pytest.raises(EvalNotScorableError, match="unsupported"):
        read_table_ref("demo", table)
