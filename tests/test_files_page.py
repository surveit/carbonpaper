"""The Files page: what a project holds, which runs read each file, and the one
irreversible thing in the app — deleting one."""
from __future__ import annotations

import hashlib
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from app.services.project import create_project
from app.core.files import files_root, list_project_files, save_upload
from run_seed import store_manifest

client = TestClient(app)

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def project_id(tmp_path, monkeypatch) -> str:
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return create_project("demo", "A methodology.", source="test").id


def store(project_id: str, name: str = "posts.csv", body: bytes = CSV):
    return save_upload(name, io.BytesIO(body), project_id)


def record_a_run(project_id: str, sha256: str, run_id: str = "20260812T120000") -> None:
    """A run that read this file, stored the way the runner stores one."""
    store_manifest(project_id, run_id,
                   {"input_bindings": {"load": {"path": "/x/posts.csv", "sha256": sha256}}})


def test_the_page_lists_what_the_project_holds(project_id):
    store(project_id)
    page = client.get(f"/project/{project_id}/files").text
    assert "posts.csv" in page
    assert CSV_SHA[:12] in page


def test_a_file_no_run_has_read_says_so(project_id):
    store(project_id)
    assert "never read" in client.get(f"/project/{project_id}/files").text


def test_the_count_comes_from_the_runs_own_manifests(project_id):
    store(project_id)
    record_a_run(project_id, CSV_SHA, "20260812T120000")
    record_a_run(project_id, CSV_SHA, "20260812T130000")
    page = client.get(f"/project/{project_id}/files").text
    assert "2 runs" in page
    # Linked to the LAST run that read it, which is the one worth opening.
    assert "/runs/20260812T130000" in page


def test_an_empty_project_says_where_files_come_from(project_id):
    page = client.get(f"/project/{project_id}/files").text
    assert "No files yet" in page


def test_the_page_leads_with_this_projects_own_files(project_id, monkeypatch):
    monkeypatch.setenv("CARBON_PAPER_FILES_QUOTA_BYTES", "1000")
    other = create_project("other", "Another methodology.", source="test").id
    store(project_id)
    store(other, name="theirs.csv", body=b"a,b\n1,2\n")
    page = client.get(f"/project/{project_id}/files").text
    # The heading is this project's; the quota is the store every project shares, and
    # says so — otherwise deleting one file here moves a number nobody can place.
    assert "<strong>13B</strong> in this project" in page
    assert "21B of 1000B across every project" in page


def test_unknown_project_404s(project_id):
    assert client.get("/project/nope/files").status_code == 404


# ─── Deleting ────────────────────────────────────────────────────────────────

def test_deleting_takes_the_filename_back(project_id):
    record = store(project_id)
    resp = client.post(f"/project/{project_id}/files/{record.id}/delete",
                       data={"confirm": "posts.csv"}, follow_redirects=False)
    assert resp.status_code == 303
    assert list_project_files(project_id) == []
    assert not (files_root() / record.id).exists()  # bytes and their dir both gone


def test_the_delete_button_starts_disabled(project_id):
    store(project_id)
    page = client.get(f"/project/{project_id}/files").text
    # A typo should be a button that will not press, not a page that says no. The 400
    # below is still the real guard; nobody should ever meet it.
    assert 'id="delete-submit" disabled' in page
    assert "Type the file\'s name to enable this" in page


def test_a_wrong_confirmation_deletes_nothing(project_id):
    record = store(project_id)
    resp = client.post(f"/project/{project_id}/files/{record.id}/delete",
                       data={"confirm": "posts"}, follow_redirects=False)
    assert resp.status_code == 400
    assert [r.filename for r in list_project_files(project_id)] == ["posts.csv"]
    assert (files_root() / record.id / "posts.csv").is_file()


def test_deleting_leaves_the_same_bytes_another_project_holds_its_own_copy_of(project_id):
    other = create_project("other", "Another methodology.", source="test").id
    mine = store(project_id)
    theirs = store(other)
    client.post(f"/project/{project_id}/files/{mine.id}/delete",
                data={"confirm": "posts.csv"})
    # Each record owns its bytes outright, so a delete takes exactly one copy and the
    # other project keeps a file it never has to share.
    assert list_project_files(project_id) == []
    assert not (files_root() / mine.id).exists()
    assert (files_root() / theirs.id / "posts.csv").is_file()


def test_deleting_a_file_this_project_does_not_hold_404s(project_id):
    assert client.post(f"/project/{project_id}/files/{'0' * 32}/delete",
                       data={"confirm": "anything"}).status_code == 404
