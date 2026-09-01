"""A run's input files link to the file page, but only where the store still holds them."""
from __future__ import annotations

import io

import pytest

from app.core.files import delete_file, files_root, resolve_stored_path, save_upload
from app.services import workspace
from app.services.project import create_project
from app.core.run_status import RunStatus
from app.models.records.run_manifest import PRODUCTION_RUNS, RunManifest
from app.web.run_index import build_run_index_rows

CSV = b"name,val\nx,1\n"


@pytest.fixture
def project_id(tmp_path, monkeypatch) -> str:
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    return create_project("demo", "A methodology.", source="test").id


def _record_run(project_id, path, filename, run_id="20260812T120000"):
    RunManifest(
        id=RunManifest.compose_id(project_id, run_id, PRODUCTION_RUNS),
        run_id=run_id, started_at="2026-08-12T12:00:00", project=project_id,
        workflow_version=None, human_review_queue_stats={},
        status=RunStatus.OK, stage_records=[],
        input_bindings={"load": {"path": str(path), "filename": filename}},
    ).save()


def _only_input(project_id):
    [row] = build_run_index_rows(project_id)
    [cell] = row.inputs
    return cell


def test_a_stored_file_links_to_its_page(project_id):
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, resolve_stored_path(record), "posts.csv")
    assert _only_input(project_id).href == f"/project/{project_id}/files/{record.id}"


def test_a_path_the_store_never_held_gets_no_link(project_id):
    _record_run(project_id, "/Users/someone/Documents/posts.csv", "posts.csv")
    assert _only_input(project_id).href is None


def test_a_deleted_file_gets_no_link(project_id):
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    path = resolve_stored_path(record)
    _record_run(project_id, path, "posts.csv")
    delete_file(project_id, record.id)
    assert _only_input(project_id).href is None


def test_another_projects_file_gets_no_link(project_id):
    other = create_project("other", "A methodology.", source="test").id
    record = save_upload("posts.csv", io.BytesIO(CSV), other)
    _record_run(project_id, resolve_stored_path(record), "posts.csv")
    assert _only_input(project_id).href is None


def test_a_store_path_naming_a_different_file_gets_no_link(project_id):
    """The directory addresses the bytes; a filename beside it that is not the record's."""
    record = save_upload("posts.csv", io.BytesIO(CSV), project_id)
    _record_run(project_id, files_root() / record.id / "other.csv", "other.csv")
    assert _only_input(project_id).href is None
