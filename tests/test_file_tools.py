"""list_files and run_workflow's `files` — how an agent finds a project's stored files
and binds one to an input step. The tools carry no bytes: a file arrives by HTTP POST to
`file_upload_url`, and everything here works from the sha256 that POST hands back."""
from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.errors import FileNotStoredError
from app.services.uploads import files_root, resolve_file_binding, save_upload
from app.tools import shared

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()
URL = "https://carbonpaper.fly.dev/project/demo/files"


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "demo").mkdir(parents=True)
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return tmp_path / "demo"


def store(name: str, body: bytes = CSV):
    return save_upload(name, io.BytesIO(body), "demo")


def test_an_empty_project_still_says_where_to_put_a_file(project):
    view = shared.list_files("demo", URL)
    assert view.files == []
    # The URL is the whole point of the call when there is nothing to list — an agent
    # that finds no file needs the link in the same breath, not a second round trip.
    assert view.file_upload_url == URL


def test_the_listing_names_each_file_by_its_hash(project):
    store("posts.csv")
    view = shared.list_files("demo", URL)
    assert [(f.sha256, f.filename, f.bytes) for f in view.files] == [
        (CSV_SHA, "posts.csv", len(CSV))
    ]


def test_newest_arrival_comes_first(project):
    store("first.csv", b"one")
    store("second.csv", b"two")
    assert [f.filename for f in shared.list_files("demo", URL).files] == [
        "second.csv", "first.csv"
    ]


def test_the_limits_ride_along_so_a_caller_knows_before_it_sends(project, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_MAX_UPLOAD_BYTES", "1000")
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "5000")
    store("posts.csv")
    view = shared.list_files("demo", URL)
    assert view.max_bytes == 1000
    assert view.remaining_bytes == 5000 - len(CSV)


def test_remaining_bytes_floors_at_zero(project, monkeypatch):
    store("posts.csv")
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "1")
    # Over quota reads as nothing left, never as a negative allowance to spend.
    assert shared.list_files("demo", URL).remaining_bytes == 0


def test_a_stored_file_resolves_to_the_params_a_run_binds(project):
    record = store("posts.csv")
    binding = resolve_file_binding("demo", CSV_SHA)
    assert binding["path"] == str(
        (files_root() / CSV_SHA / "posts.csv").resolve())
    # The format comes off the stored name's extension, so a binding cannot leave a
    # csv to be read by whatever format the workflow authored.
    assert binding["format"] == "csv"
    assert record.sha256 == CSV_SHA


def test_an_unknown_file_id_fails_naming_itself(project):
    with pytest.raises(FileNotStoredError, match="has no file"):
        resolve_file_binding("demo", "0" * 64)


def test_a_record_whose_bytes_are_gone_fails_before_the_run_starts(project):
    store("posts.csv")
    (files_root() / CSV_SHA / "posts.csv").unlink()
    # Worse than no record: the run would bind a path and fail at preflight, naming a
    # file the caller was just told the project had.
    with pytest.raises(FileNotStoredError, match="bytes are not on disk"):
        resolve_file_binding("demo", CSV_SHA)


def test_an_unknown_project_is_loud(project):
    with pytest.raises(ValueError, match="no project 'nope'"):
        shared.list_files("nope", URL)
