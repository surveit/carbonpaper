from __future__ import annotations

from app.models import validate_workflow_draft
from conftest import source_stage


def _report_stage(*, one_file_per):
    return {
        "id": "pub", "type": "report", "description": "pub",
        "inputs": [{"id": "src"}],
        "signature": {"form": "replaces"},
        "report": {"one_file_per": one_file_per},
        "function": {"kind": "inline", "code": "def transform(df, output_dir):\n    return df"},
    }


def _issues(*, one_file_per, edge_columns):
    return "; ".join(validate_workflow_draft([
        source_stage("src", [{"name": c, "type": "str", "nullable": True} for c in edge_columns]),
        _report_stage(one_file_per=one_file_per),
    ]))


def test_one_file_per_missing_rejected():
    assert "one_file_per" in _issues(one_file_per="nope", edge_columns=["a"])


def test_one_file_per_present_ok():
    assert _issues(one_file_per="a", edge_columns=["a"]) == ""


def test_one_file_per_unset_is_clean():
    assert _issues(one_file_per=None, edge_columns=["a"]) == ""
