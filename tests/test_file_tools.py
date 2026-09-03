"""list_files and run_workflow's `files` — how an agent finds a project's stored files
and binds one to an input step. The tools carry no bytes: a file arrives by HTTP POST to
`file_upload_url`, and everything here works from the file id that POST hands back."""
from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.core import files as file_store
from app.core.errors import FileNotStoredError, StoreOverQuota
from app.core.files import files_root, save_upload
from app.services.project import save_working_copy_as_version
from app.services.uploads import resolve_files_binding
from app.tools import shared
from stage_seed import add_stage

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


def test_the_listing_names_each_file_by_its_record(project):
    record = store("posts.csv")
    view = shared.list_files("demo", URL)
    assert [(f.file_id, f.filename, f.bytes) for f in view.files] == [
        (record.id, "posts.csv", len(CSV))
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
    binding = resolve_files_binding("demo", [record.id])
    assert binding["paths"][0] == str(
        (files_root() / record.id / "posts.csv").resolve())
    # The format comes off the stored name's extension, so a binding cannot leave a
    # csv to be read by whatever format the workflow authored.
    assert binding["format"] == "csv"
    # The hash is still recorded, as evidence about the bytes rather than their address.
    assert record.sha256 == CSV_SHA


def test_an_unknown_file_id_fails_naming_itself(project):
    with pytest.raises(FileNotStoredError, match="has no file"):
        resolve_files_binding("demo", ["0" * 32])


def test_a_files_own_sha256_no_longer_names_it_to_a_run(project):
    """The hash addressed the bytes until this store held provenance; now the record does."""
    store("posts.csv")
    with pytest.raises(FileNotStoredError, match="has no file"):
        resolve_files_binding("demo", [CSV_SHA])


def test_a_record_whose_bytes_are_gone_fails_before_the_run_starts(project):
    record = store("posts.csv")
    (files_root() / record.id / "posts.csv").unlink()
    # Worse than no record: the run would bind a path and fail at preflight, naming a
    # file the caller was just told the project had.
    with pytest.raises(FileNotStoredError, match="bytes are not on disk"):
        resolve_files_binding("demo", [record.id])


def test_run_workflow_binds_several_files_to_one_input_as_one_table(project, monkeypatch):
    """`files` widened to take a list: several stored files read by one input stage."""
    import app.services.run as run_service

    monkeypatch.setattr(run_service, "_run_in_background", lambda target, *args: target(*args))
    first = store("q1.csv", b"name,val\nx,1\n")
    second = store("q2.csv", b"name,val\ny,2\n")
    add_stage(project, {
        "id": "load", "description": "Load rows", "type": "input_data",
        "connector": {"kind": "file", "params": {"format": "csv"}},
        "signature": {
            "form": "replaces",
            "produces": [
                {"name": "name", "type": "str", "nullable": True},
                {"name": "val", "type": "int", "nullable": True},
            ],
        },
    })
    save_working_copy_as_version("demo", message="seed")

    result = shared.run_workflow("demo", files={"load": [first.id, second.id]})

    status = run_service.read_run_status("demo", result["run_id"])
    assert status["status"] == "ok"
    assert status["stage_records"][0]["output_row_count"] == 2


def test_an_unknown_project_is_loud(project):
    with pytest.raises(ValueError, match="no project 'nope'"):
        shared.list_files("nope", URL)


def test_the_same_bytes_in_two_projects_are_weighed_twice(project, tmp_path):
    """Each record owns its own copy, so the quota counts both. Provenance costs disk."""
    (tmp_path / "other").mkdir(parents=True)
    first = save_upload("posts.csv", io.BytesIO(CSV), "demo")
    second = save_upload("posts.csv", io.BytesIO(CSV), "other")
    assert len(file_store.ProjectFile.list()) == 2
    assert file_store.resolve_stored_path(first) != file_store.resolve_stored_path(second)
    assert file_store.measure_files_used_bytes() == 2 * len(CSV)


def test_what_is_used_is_read_off_the_records_not_the_disk(project):
    """A directory no record covers is another workspace's; this store does not own its bytes."""
    save_upload("posts.csv", io.BytesIO(CSV), "demo")
    orphan = files_root() / ("f" * 32)
    orphan.mkdir(parents=True)
    (orphan / "stray.csv").write_bytes(b"x" * 4096)
    assert file_store.measure_files_used_bytes() == len(CSV)


def test_the_arriving_file_counts_against_the_quota_before_it_has_a_record(
    project, monkeypatch
):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", str(len(CSV) + 1))
    save_upload("posts.csv", io.BytesIO(CSV), "demo")
    # The second file has no record while it is being weighed, so a records-only count
    # would let it through: together they are over, and it is refused.
    with pytest.raises(StoreOverQuota):
        save_upload("more.csv", io.BytesIO(b"name,val\ny,2\n"), "demo")


def test_re_sending_bytes_the_store_already_holds_costs_quota_like_any_other_send(
    project, monkeypatch
):
    save_upload("posts.csv", io.BytesIO(CSV), "demo")
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", str(len(CSV)))
    # A second copy lands on disk, so the same bytes again are refused at quota. That is
    # the cost of every send keeping its own record of where it came from.
    with pytest.raises(StoreOverQuota):
        save_upload("posts-again.csv", io.BytesIO(CSV), "demo")


def test_the_same_bytes_under_a_new_name_land_in_their_own_directory(project):
    """Each record owns a directory holding exactly the one file its `filename` names."""
    first = save_upload("posts.csv", io.BytesIO(CSV), "demo")
    again = save_upload("posts-renamed.csv", io.BytesIO(CSV), "demo")
    assert first.id != again.id
    assert again.sha256 == first.sha256  # the hash is evidence, not the address
    for record, name in ((first, "posts.csv"), (again, "posts-renamed.csv")):
        assert [f.name for f in (files_root() / record.id).iterdir()] == [name]
        assert file_store.resolve_stored_path(record).read_bytes() == CSV
