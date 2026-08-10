"""POST /project/{name}/upload-input — the browser-native file picker behind the
run form's Browse… button. The browser hands over bytes (no path), so the server
saves them under uploads/<sha256>/<filename> and returns that copy's absolute
path, which the run then reads in place like any other input."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.uploads import UploadedFile

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    return proj


def upload(name: str, body: bytes, project_name: str = "demo"):
    return client.post(
        f"/project/{project_name}/upload-input",
        files={"file": (name, body, "text/csv")},
    )


def test_upload_saves_under_the_content_hash_and_returns_the_path(project):
    body = upload("posts.csv", CSV).json()
    assert body["ok"] is True
    saved = Path(body["path"])
    assert saved == (project / "uploads" / CSV_SHA / "posts.csv").resolve()
    assert saved.read_bytes() == CSV  # bytes landed intact


def test_the_stored_filename_keeps_the_extension_a_binding_reads_the_format_from(project):
    saved = Path(upload("2026-lobbying.xlsx", b"PK\x03\x04").json()["path"])
    # _collect_bindings resolves the run's file format off this suffix, so a
    # hash-named copy with no extension would fail the trigger.
    assert saved.suffix == ".xlsx"
    assert saved.name == "2026-lobbying.xlsx"


def test_same_bytes_twice_is_one_copy(project):
    first = upload("posts.csv", CSV).json()["path"]
    second = upload("posts.csv", CSV).json()["path"]
    assert first == second
    assert [p.name for p in (project / "uploads" / CSV_SHA).iterdir()] == ["posts.csv"]


def test_different_bytes_do_not_overwrite_each_other(project):
    one = Path(upload("a.csv", b"one").json()["path"])
    two = Path(upload("a.csv", b"two").json()["path"])
    assert one != two
    assert one.read_bytes() == b"one" and two.read_bytes() == b"two"


def test_no_temp_file_is_left_behind(project):
    upload("posts.csv", CSV)
    leftovers = [p.name for p in (project / "uploads").iterdir() if p.name.startswith(".")]
    assert leftovers == []


def test_the_record_names_the_file_the_human_picked(project):
    upload("posts.csv", CSV)
    record = UploadedFile.load(f"demo/{CSV_SHA}")
    assert record.sha256 == CSV_SHA
    assert record.filename == "posts.csv"
    assert record.byte_count == len(CSV)


def test_re_picking_the_same_bytes_keeps_the_first_arrival_time(project):
    upload("posts.csv", CSV)
    first = UploadedFile.load(f"demo/{CSV_SHA}")
    upload("renamed.csv", CSV)
    again = UploadedFile.load(f"demo/{CSV_SHA}")
    assert again.created_at == first.created_at  # same bytes, first seen once
    assert again.filename == "renamed.csv"       # the latest pick names it
    assert again.updated_at > first.updated_at


def test_filename_is_basename_sanitized(project):
    # A crafted name must not escape the hash dir it is written to.
    saved = Path(upload("../../etc/evil.csv", b"x").json()["path"])
    assert saved.parent.parent == (project / "uploads").resolve()
    assert saved.name == "evil.csv"


def test_a_nameless_upload_still_stores(project):
    # Path("..").name is "..", which would climb out of the hash dir.
    saved = Path(upload("..", b"x").json()["path"])
    assert saved.name == "upload.dat"
    assert saved.parent.parent == (project / "uploads").resolve()


def test_missing_file_is_422(project):
    # FastAPI rejects a missing required File before the handler runs.
    assert client.post("/project/demo/upload-input").status_code == 422


def test_unknown_project_404(project):
    assert upload("a.csv", b"x", project_name="nope").status_code == 404
