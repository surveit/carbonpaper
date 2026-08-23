"""One file's page: its shape, the completeness a reader claims, and the runs that read it."""
from __future__ import annotations

import hashlib
import io

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.core.files import FileCompleteness, ProjectFile, save_upload
from app.main import app
from app.services import workspace
from app.services.project import create_project
from run_seed import store_manifest

client = TestClient(app)

# Two columns as a social export arrives: one geolocated, one empty in every row.
CSV = (b"Country,Hashtags,Reactions\n"
       b"Netherlands,,4\n"
       b",,0\n"
       b"Poland,,0\n"
       b"Netherlands,,120\n")
CSV_SHA = hashlib.sha256(CSV).hexdigest()


@pytest.fixture
def project_id(tmp_path, monkeypatch) -> str:
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return create_project("demo", "A methodology.", source="test").id


@pytest.fixture
def file_id(project_id) -> str:
    return save_upload("posts.csv", io.BytesIO(CSV), project_id).id


def page(project_id: str, file_id: str) -> str:
    response = client.get(f"/project/{project_id}/files/{file_id}")
    assert response.status_code == 200, response.text
    return response.text


def test_the_files_table_links_each_row_to_its_own_page(project_id, file_id):
    listing = client.get(f"/project/{project_id}/files").text
    assert f"/project/{project_id}/files/{file_id}" in listing


def test_the_page_names_the_file_and_its_shape(project_id, file_id):
    text = page(project_id, file_id)
    assert "posts.csv" in text
    assert "4 rows × 3 columns" in text


def test_a_column_holding_nothing_is_counted_as_empty(project_id, file_id):
    # A csv's missing field arrives as a null, so this column reads as null in every row.
    text = page(project_id, file_id)
    assert "1 hold no value at all" in text
    assert "null in every row" in text


def test_an_empty_string_is_empty_too_and_ranks_among_the_values(project_id, tmp_path):
    # A null count alone would call this column full.
    frame = pd.DataFrame({"Country": ["Netherlands", "", "", "Poland"]})
    path = tmp_path / "export.parquet"
    frame.to_parquet(path)
    file_id = save_upload("export.parquet", io.BytesIO(path.read_bytes()), project_id).id
    text = page(project_id, file_id)
    assert "(empty string)" in text
    assert "2 rows that carry a value" in text


def test_a_file_no_run_has_read_says_so(project_id, file_id):
    assert "No run has read this file" in page(project_id, file_id)


def test_a_run_that_read_the_bytes_is_listed(project_id, file_id):
    store_manifest(project_id, "20260812T120000",
                   {"input_bindings": {"load": {"path": "/x/posts.csv", "sha256": CSV_SHA}},
                    "status": "done", "started_at": "2026-08-12T12:00:00"})
    text = page(project_id, file_id)
    assert "20260812T120000" in text
    assert "1 run\n" in text or "1 run " in text


def test_saving_completeness_and_the_note_keeps_them(project_id, file_id):
    response = client.post(
        f"/project/{project_id}/files/{file_id}/provenance",
        data={"completeness": "closed", "lineage": "Every filing FOIA returned."},
        follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith(f"/files/{file_id}")
    record = ProjectFile.load(file_id)
    assert record.completeness == FileCompleteness.CLOSED
    assert record.lineage == "Every filing FOIA returned."


def test_a_sampled_file_is_refused_without_a_note_saying_how(project_id, file_id):
    response = client.post(f"/project/{project_id}/files/{file_id}/provenance",
                           data={"completeness": "sampled", "lineage": "  "})
    assert response.status_code == 422
    assert "how the sample was drawn" in response.text
    assert ProjectFile.load(file_id).completeness == FileCompleteness.OPEN


def test_a_file_this_project_does_not_hold_is_not_found(project_id):
    other = create_project("other", "A methodology.", source="test").id
    elsewhere = save_upload("theirs.csv", io.BytesIO(CSV), other).id
    assert client.get(f"/project/{project_id}/files/{elsewhere}").status_code == 404


def test_a_file_that_is_not_a_table_still_has_a_page(project_id):
    # A png someone attached to a conversation: no shape, but still a record to act on.
    file_id = save_upload("screenshot.png", io.BytesIO(b"\x89PNG\r\n\x1a\n"), project_id).id
    text = page(project_id, file_id)
    assert "This file is not a table" in text
    assert "Delete this file" in text
    assert "Data completeness" in text


def test_a_section_that_opens_says_so(project_id, file_id):
    # Beside Read by and Delete, which are headings and do not open.
    text = page(project_id, file_id)
    assert text.count('<details class="file-rows') == 2
    assert '<div class="file-fold">' in text
