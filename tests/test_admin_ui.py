"""Isolated by repointing both workspace.EXAMPLES_DIR and admin.py's own captured
REPO_ROOT; patching only one leaves the tests writing into the real workspace.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.web.routers.admin as admin_router
from app.main import app
from app.services import project, workspace
from app.services.project import WorkflowFile

client = TestClient(app)

_LOBBYING = "lobbying_issue_triage"


@pytest.fixture(autouse=True)
def workspace_root(tmp_path, monkeypatch):
    """A fresh examples/ root (workspace.EXAMPLES_DIR) plus its containing repo
    root (admin_router.REPO_ROOT, the base for exported bundles) — the same
    examples/ + exports/ layout the real repo has, so path handling in
    export_project matches production."""
    examples_dir = tmp_path / "examples"
    examples_dir.mkdir()
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", examples_dir)
    monkeypatch.setattr(admin_router, "REPO_ROOT", tmp_path, raising=False)
    return examples_dir


def test_admin_page_lists_the_seed_bundle():
    r = client.get("/admin")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert _LOBBYING in r.text


def test_load_bundle_redirects_and_the_project_appears(workspace_root):
    r = client.post(f"/admin/load/{_LOBBYING}", follow_redirects=False)

    assert r.status_code == 303
    assert r.headers["location"].startswith("/admin")
    assert _LOBBYING in project.list_projects(examples_dir=workspace_root)
    assert _LOBBYING in client.get("/admin").text


def test_loading_the_same_bundle_twice_does_not_crash(workspace_root):
    first = client.post(f"/admin/load/{_LOBBYING}", follow_redirects=False)
    second = client.post(f"/admin/load/{_LOBBYING}", follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert _LOBBYING in project.list_projects(examples_dir=workspace_root)


def test_export_project_writes_a_workflow_file_json(workspace_root):
    client.post(f"/admin/load/{_LOBBYING}", follow_redirects=False)

    r = client.post(f"/admin/export/{_LOBBYING}", follow_redirects=False)

    assert r.status_code == 303
    dest = admin_router.REPO_ROOT / "exports" / f"{_LOBBYING}.json"
    assert dest.is_file()
    wf = WorkflowFile.model_validate_json(dest.read_text(encoding="utf-8"))
    assert wf.name == _LOBBYING
    assert {stage.id for stage in wf.stages} == {
        "raw_filings", "classify_issues", "rank_by_spend", "publish_report",
    }


def test_load_unknown_bundle_is_a_clean_404():
    r = client.post("/admin/load/does_not_exist", follow_redirects=False)
    assert r.status_code == 404


def test_export_unknown_project_is_a_clean_404():
    r = client.post("/admin/export/does_not_exist", follow_redirects=False)
    assert r.status_code == 404
