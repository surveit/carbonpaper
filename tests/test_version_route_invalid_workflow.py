"""Regression from dogfooding (palm_oil_mill_osint, 2026-07-20): a workflow with 18
validation issues returned a bare 500, discarding the itemized report.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.services.project import create_project
from test_journey_smoke import _point_examples_dir_at

client = TestClient(app)


def test_invalid_working_copy_versions_as_400_with_issues(tmp_path, monkeypatch):
    _point_examples_dir_at(tmp_path)
    project_id = create_project("relpath", "Load a file.", source="test").id
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": "data/things.csv", "format": "csv"}}}
    project_dir = tmp_path / project_id
    (project_dir / "compiled").mkdir()
    (project_dir / "compiled" / "01_load.json").write_text(
        json.dumps(stage), encoding="utf-8")

    resp = client.post(f"/project/{project_id}/version",
                       data={"message": "should fail loudly"})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "working copy failed validation" in body["detail"]
    assert any("ABSOLUTE" in issue for issue in body["issues"]), body
