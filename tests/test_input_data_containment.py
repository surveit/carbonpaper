"""Fix for the arbitrary-file-read in the input_data connector (issue #100): a
connector `path` must stay inside the project/repo root. An absolute path or a
`..` traversal that escapes the anchor is rejected loudly, not silently followed.
"""
from __future__ import annotations

import pytest

from app.models import Stage
from app.runtime.stages.input_data import handle_input_data


def _file_stage(path: str) -> Stage:
    return Stage.model_validate({
        "id": "in", "name": "in", "type": "input_data",
        "inputs": [],
        "connector": {"kind": "file", "params": {"path": path, "format": "csv"}},
        "output_schema": {"columns": [{"name": "a", "type": "str"}]},
    })


def _computed_static_stage(file: str) -> Stage:
    return Stage.model_validate({
        "id": "in", "name": "in", "type": "input_data",
        "inputs": [],
        "connector": {"kind": "computed_static", "params": {"file": file}},
        "output_schema": {"columns": [{"name": "a", "type": "str"}]},
    })


def test_reads_a_contained_file(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "in.csv").write_text("a\n1\n2\n", encoding="utf-8")
    df = handle_input_data(_file_stage("data/in.csv"), {}, {"repo_root": tmp_path})
    assert list(df["a"]) == [1, 2]


def test_rejects_parent_traversal(tmp_path):
    # A secret one level above the anchor must be unreachable via `..`.
    (tmp_path.parent / "secret.csv").write_text("a\n9\n", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes the project root"):
        handle_input_data(_file_stage("../secret.csv"), {}, {"repo_root": tmp_path})


def test_rejects_deep_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes the project root"):
        handle_input_data(_file_stage("../../../etc/passwd"), {}, {"repo_root": tmp_path})


def test_rejects_absolute_path(tmp_path):
    with pytest.raises(ValueError, match="escapes the project root"):
        handle_input_data(_file_stage("/etc/passwd"), {}, {"repo_root": tmp_path})


def test_computed_static_rejects_traversal(tmp_path):
    with pytest.raises(ValueError, match="escapes the project root"):
        handle_input_data(_computed_static_stage("../secret.csv"), {}, {"repo_root": tmp_path})


def test_computed_static_reads_contained_file(tmp_path):
    (tmp_path / "snap.csv").write_text("a\n7\n", encoding="utf-8")
    df = handle_input_data(_computed_static_stage("snap.csv"), {}, {"repo_root": tmp_path})
    assert list(df["a"]) == [7]
