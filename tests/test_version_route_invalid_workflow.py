"""POST /project/{p}/version on a working copy that fails validation must
return the validation issues as a structured 400 — the workflow page's save
handler renders `issues` to the reviewer — never a bare 500 that hides them.

Found by dogfooding (palm_oil_mill_osint, 2026-07-20): a legacy generated
workflow with 18 validation issues produced `Internal Server Error` on
version-save, discarding the itemized report the loader had already built.
"""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.main import app
from app.services.project import create_project
from test_journey_smoke import _point_examples_dir_at

client = TestClient(app)


def test_invalid_working_copy_versions_as_400_with_issues(tmp_path, monkeypatch):
    _point_examples_dir_at(monkeypatch, tmp_path)
    create_project("relpath", "Load a file.", source="test")
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": "data/things.csv", "format": "csv"}}}
    project_dir = tmp_path / "relpath"
    (project_dir / "compiled").mkdir()
    (project_dir / "compiled" / "01_load.json").write_text(
        json.dumps(stage), encoding="utf-8")

    resp = client.post("/project/relpath/version", data={"message": "should fail loudly"})

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "working copy failed validation" in body["detail"]
    assert any("ABSOLUTE" in issue for issue in body["issues"]), body
