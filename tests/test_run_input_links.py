"""A run's input files link to the file page, joined on bytes as the lineage tab joins them."""
from __future__ import annotations

import hashlib
import io

import pytest

from app.core.files import delete_file, save_upload
from app.core.run_status import RunStatus
from app.models.records.run_manifest import PRODUCTION_RUNS, RunManifest
from app.services import workspace
from app.services.project import create_project
from app.web.run_index import build_run_index_rows

CSV = b"name,val\nx,1\n"
CSV_SHA = hashlib.sha256(CSV).hexdigest()
OTHER = b"name,val\ny,2\n"
OTHER_SHA = hashlib.sha256(OTHER).hexdigest()


@pytest.fixture
def project_id(tmp_path, monkeypatch) -> str:
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return create_project("demo", "A methodology.", source="test").id


def _record_run(project_id, sha256, run_id="20260812T120000", path="/anywhere/posts.csv"):
    RunManifest(
        id=RunManifest.compose_id(project_id, run_id, PRODUCTION_RUNS),
        run_id=run_id, started_at="2026-08-12T12:00:00", project=project_id,
        workflow_version=None, human_review_queue_stats={},
        status=RunStatus.OK, stage_records=[],
        input_bindings={"load": {"path": path, "filename": "posts.csv", "sha256": sha256}},
    ).save()


def _only_input(project_id):
    [row] = build_run_index_rows(project_id)
    [cell] = row.inputs
    return cell


def test_a_stored_file_links_to_its_page(project_id):
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, CSV_SHA)
    assert _only_input(project_id).href == f"/project/{project_id}/files/{record.id}"


def test_a_path_outside_the_store_still_links_on_its_bytes(project_id):
    """A run reads where the operator points it; the store holds a copy of the same bytes."""
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, CSV_SHA, path="/Users/someone/Documents/posts.csv")
    assert _only_input(project_id).href == f"/project/{project_id}/files/{record.id}"


def test_bytes_this_project_does_not_hold_get_no_link(project_id):
    save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, OTHER_SHA)
    assert _only_input(project_id).href is None


def test_a_binding_recording_no_hash_gets_no_link(project_id):
    save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, None)
    assert _only_input(project_id).href is None


def test_a_deleted_file_gets_no_link(project_id):
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, CSV_SHA)
    delete_file(project_id, record.id)
    assert _only_input(project_id).href is None


def test_another_projects_file_gets_no_link(project_id):
    other = create_project("other", "A methodology.", source="test").id
    save_upload("posts.csv", io.BytesIO(CSV), other)
    _record_run(project_id, CSV_SHA)
    assert _only_input(project_id).href is None


def test_the_same_bytes_sent_twice_link_to_the_newer_record(project_id):
    """Re-sending a file makes a second record; the link names the one shown first."""
    save_upload("posts.csv", io.BytesIO(CSV), project_id)
    newer = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, CSV_SHA)
    assert _only_input(project_id).href == f"/project/{project_id}/files/{newer.id}"
