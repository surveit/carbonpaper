"""The Workflow page when there is nothing to report.

Silence is what a reviewer sees when nobody has looked, so a clean workflow has to
say so — and a workflow that never loaded must not say it.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.services.project import create_project
from test_journey_smoke import _point_examples_dir_at

client = TestClient(app)

_SCHEMA = {"columns": [{"name": "id", "type": "str"}], "primary_key": ["id"]}
_CLEAN_LINE = "0 errors, 0 warnings"

_LOAD = {
    "id": "load", "name": "Load", "type": "input_data",
    "output_schema": _SCHEMA,
    "connector": {"kind": "file",
                  "params": {"path": "/data/things.csv", "format": "csv"}},
}
_UNDESCRIBED = {
    "id": "shape", "name": "Shape", "type": "python_row_function",
    "inputs": [{"id": "load", "schema": _SCHEMA}],
    "output_schema": _SCHEMA,
    "function": {"kind": "inline", "code": "def transform(row):\n    return row"},
}


def _workflow_page(tmp_path, name, stages):
    _point_examples_dir_at(tmp_path)
    create_project(name, "A workflow.", source="test")
    compiled = tmp_path / name / "compiled"
    compiled.mkdir()
    for position, stage in enumerate(stages, start=1):
        (compiled / f"{position:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")
    resp = client.get(f"/project/{name}/workflow")
    assert resp.status_code == 200, resp.text
    return resp.text


def test_a_workflow_with_no_warnings_says_so(tmp_path):
    page = _workflow_page(tmp_path, "clean", [_LOAD])
    assert _CLEAN_LINE in page
    assert "wf-issues-clean" in page


def test_a_workflow_with_a_warning_lists_it_instead(tmp_path):
    page = _workflow_page(tmp_path, "dirty", [_LOAD, _UNDESCRIBED])
    assert _CLEAN_LINE not in page
    assert "to fix before signing this workflow off" in page


def test_a_workflow_that_does_not_load_claims_nothing(tmp_path):
    """Zero typed stages produce zero warnings, which is not a clean bill of health."""
    # A relative connector path is rejected by input_data, so nothing types.
    broken = {**_LOAD, "connector": {"kind": "file",
                                     "params": {"path": "data/things.csv",
                                                "format": "csv"}}}
    page = _workflow_page(tmp_path, "broken", [broken])
    assert _CLEAN_LINE not in page
    assert "wf-issues" not in page
