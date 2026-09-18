"""The Workflow page when there is nothing to report.

Silence is what a reviewer sees when nobody has looked, so a clean workflow has to
say so — and a workflow that never loaded must not say it.
"""
from __future__ import annotations


from fastapi.testclient import TestClient

from app.main import app
from app.services.project import create_project
from test_journey_smoke import _point_examples_dir_at
from stage_seed import add_stage

client = TestClient(app)

_SCHEMA = {"columns": [{"name": "id", "type": "str", "nullable": True}]}
_CLEAN_LINE = "0 errors, 0 warnings"

_UNDESCRIBED = {
    "id": "shape", "description": "Shape", "type": "python_row_function",
    "inputs": [{"id": "load"}],
    "signature": {"form": "extends", "reads": [{"input": "load", "columns": _SCHEMA["columns"]}]},
    "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
}


def _make_load_stage(path):
    return {
        "id": "load", "description": "Load", "type": "input_data",
        # A source's rows are a new kind of thing, so a clean one names what they are.
        "row_type_id": "thing",
        "signature": {"form": "replaces", "produces": _SCHEMA["columns"]},
        "connector": {"kind": "file", "params": {"path": path, "format": "csv"}},
    }


def _workflow_page(tmp_path, name, stages):
    _point_examples_dir_at(tmp_path)
    project_id = create_project(name, "A workflow.", source="test").id
    compiled = tmp_path / project_id
    compiled.mkdir(parents=True, exist_ok=True)
    for position, stage in enumerate(stages, start=1):
        add_stage(compiled, stage)
    resp = client.get(f"/project/{project_id}/workflow")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_a_workflow_with_no_warnings_says_so(tmp_path):
    load = _make_load_stage(str(tmp_path / "things.csv"))
    page = _workflow_page(tmp_path, "clean", [load])
    assert _CLEAN_LINE in page
    assert "wf-issues-clean" in page


def test_a_workflow_with_a_warning_lists_it_instead(tmp_path):
    load = _make_load_stage(str(tmp_path / "things.csv"))
    page = _workflow_page(tmp_path, "dirty", [load, _UNDESCRIBED])
    assert _CLEAN_LINE not in page
    # A missing description is a warning, so the panel counts it as one; the count
    # it does not have goes unwritten.
    assert "1 warning" in page
    assert "0 errors" not in page


def test_a_workflow_whose_rows_go_unnamed_is_counted_as_an_error(tmp_path):
    unnamed = {**_make_load_stage(str(tmp_path / "things.csv"))}
    del unnamed["row_type_id"]
    page = _workflow_page(tmp_path, "unnamed", [unnamed])

    assert "1 error" in page
    assert "1 warning" not in page
    assert "issue-panel-error" in page


def test_errors_and_warnings_are_counted_apart(tmp_path):
    unnamed = {**_make_load_stage(str(tmp_path / "things.csv"))}
    del unnamed["row_type_id"]
    page = _workflow_page(tmp_path, "both", [unnamed, _UNDESCRIBED])

    assert "1 warning, 1 error" in page


def test_a_workflow_that_does_not_load_claims_nothing(tmp_path):
    # A relative connector path is rejected by input_data, so nothing types.
    page = _workflow_page(tmp_path, "broken", [_make_load_stage("data/things.csv")])
    assert _CLEAN_LINE not in page
    assert "wf-issues" not in page
