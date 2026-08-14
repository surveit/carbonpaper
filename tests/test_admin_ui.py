"""Isolated by repointing the projects root, so the tests never read or write
the real workspace.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import project, workspace
from app.services.project import WorkflowFile

client = TestClient(app)

_BUNDLE = "tutorial_lobbying_triage"


@pytest.fixture(autouse=True)
def workspace_root(tmp_path):
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    workspace.set_projects_dir(examples_dir)
    return examples_dir


def test_admin_page_lists_the_seed_bundle():
    r = client.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert _BUNDLE in r.text


def _loaded_project_id() -> str:
    """The bundle's label is not its id, so ask the store which project it became."""
    [record] = project.find_projects_by_name(_BUNDLE)
    return record.id


def test_load_bundle_redirects_and_the_project_appears(workspace_root):
    r = client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin")
    assert _loaded_project_id() in project.list_projects()
    assert _BUNDLE in client.get("/admin").text


def test_loading_the_same_bundle_twice_makes_two_projects(workspace_root):
    """A label is not unique, so the second load is a second project, not a refusal."""
    first = client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)
    second = client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert len(project.find_projects_by_name(_BUNDLE)) == 2
    assert len(project.list_projects()) == 2


def test_download_returns_the_workflow_file_as_an_attachment(workspace_root):
    client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)

    r = client.get(f"/admin/export/{_loaded_project_id()}")

    assert r.status_code == 200
    assert r.headers["content-disposition"] == f'attachment; filename="{_BUNDLE}.json"'
    wf = WorkflowFile.model_validate_json(r.content)
    assert wf.name == _BUNDLE
    assert [stage.id for stage in wf.stages] == [
        "raw_filings", "public_commitments", "check_filings",
        "matched_commitments", "judge_alignment", "review_contradictions",
        "publish_report",
    ]


def test_load_unknown_bundle_is_a_clean_404():
    r = client.post("/admin/load/does_not_exist", follow_redirects=False)
    assert r.status_code == 404


def test_download_unknown_project_is_a_clean_404():
    r = client.get("/admin/export/does_not_exist")
    assert r.status_code == 404


# ─── Upload ───────────────────────────────────────────────────────────────────


def _upload(payload: bytes, filename: str = "bundle.json"):
    return client.post(
        "/admin/import",
        files={"file": (filename, payload, "application/json")},
        follow_redirects=False,
    )


def test_a_downloaded_bundle_uploads_back_into_an_empty_workspace(workspace_root):
    client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)
    downloaded = client.get(f"/admin/export/{_loaded_project_id()}").content
    _empty_workspace(workspace_root.parent / "second")

    r = _upload(downloaded)

    assert r.status_code == 303
    # A fresh workspace mints a NEW id for it, and the bundle round-trips unchanged.
    reimported = _loaded_project_id()
    assert client.get(f"/admin/export/{reimported}").content == downloaded


def test_uploading_a_bundle_whose_label_is_taken_leaves_the_first_alone(workspace_root):
    client.post(f"/admin/load/{_BUNDLE}", follow_redirects=False)
    first_id = _loaded_project_id()
    downloaded = client.get(f"/admin/export/{first_id}").content

    r = _upload(downloaded)

    assert r.status_code == 303
    # A second project, and the first is untouched — nothing was overwritten to make room.
    assert len(project.find_projects_by_name(_BUNDLE)) == 2
    assert client.get(f"/admin/export/{first_id}").content == downloaded


def _empty_workspace(root):
    """A fresh directory is not enough on its own — project identity is a store record."""
    from app.core.persistence import configure_store
    from app.core.sqlite_store import SqliteKvStore

    root.mkdir(parents=True, exist_ok=True)
    workspace.set_projects_dir(root)
    configure_store(SqliteKvStore(":memory:"))


@pytest.mark.parametrize(
    "payload",
    [
        b"{not json at all",
        b'{"name": "half_a_bundle", "document": "hi"}',
        b'{"name": "bad_stage", "document": "hi", "model": "sonnet", "source": "test",'
        b' "data_model": {"schemas": []}, "stages": [{"id": "s", "type": "not_a_type"}]}',
    ],
    ids=["unparseable", "missing_fields", "unknown_stage_type"],
)
def test_a_malformed_upload_400s_and_writes_no_project(payload):
    r = _upload(payload)

    assert r.status_code == 400
    assert "not a valid WorkflowFile document" in r.json()["detail"]
    assert project.list_projects() == []
