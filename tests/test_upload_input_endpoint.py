"""POST /project/{name}/upload-input — the browser-native file picker behind the
run form's Browse… button. The browser hands over bytes (no path), so the server
saves them under uploads/<stage_id>/ and returns the saved copy's absolute path,
which the run then reads in place like any other input."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
from app.main import app

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True)
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    return proj


def test_upload_saves_under_stage_dir_and_returns_path(project):
    resp = client.post(
        "/project/demo/upload-input",
        data={"stage_id": "load"},
        files={"file": ("posts.csv", b"name,val\nx,1\n", "text/csv")},
    )
    body = resp.json()
    assert body["ok"] is True
    saved = Path(body["path"])
    assert saved == (project / "uploads" / "load" / "posts.csv").resolve()
    assert saved.read_bytes() == b"name,val\nx,1\n"  # bytes landed intact


def test_reupload_same_stage_and_name_overwrites(project):
    def files(b):
        return {"file": ("a.csv", b, "text/csv")}
    p1 = client.post("/project/demo/upload-input", data={"stage_id": "s"},
                     files=files(b"one")).json()["path"]
    p2 = client.post("/project/demo/upload-input", data={"stage_id": "s"},
                     files=files(b"two")).json()["path"]
    assert p1 == p2 and Path(p2).read_bytes() == b"two"  # replaced in place


def test_filename_is_basename_sanitized(project):
    # A crafted name must not escape the uploads/<stage> dir.
    resp = client.post(
        "/project/demo/upload-input",
        data={"stage_id": "load"},
        files={"file": ("../../etc/evil.csv", b"x", "text/csv")},
    )
    saved = Path(resp.json()["path"])
    assert saved == (project / "uploads" / "load" / "evil.csv").resolve()
    assert (project / "uploads" / "load" / "evil.csv").exists()


def test_stage_id_cannot_traverse(project):
    resp = client.post(
        "/project/demo/upload-input",
        data={"stage_id": "../../.."},
        files={"file": ("a.csv", b"x", "text/csv")},
    )
    saved = Path(resp.json()["path"])
    assert (project / "uploads") in saved.parents  # stayed inside uploads/


def test_missing_file_is_422(project):
    # FastAPI rejects a missing required File before the handler runs.
    resp = client.post("/project/demo/upload-input", data={"stage_id": "load"})
    assert resp.status_code == 422


def test_unknown_project_404(project):
    resp = client.post(
        "/project/nope/upload-input",
        data={"stage_id": "load"},
        files={"file": ("a.csv", b"x", "text/csv")},
    )
    assert resp.status_code == 404
