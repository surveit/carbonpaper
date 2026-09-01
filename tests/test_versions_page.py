"""Route tests for the versions LIST page: it lists what has been cut and never
changes anything
and redirects to that detail page, and the run trigger refuses only a project with
no stored version."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import project as project_service
from app.services import workspace
from stage_seed import add_stage

client = TestClient(app)

# Every non-report stage declares its output_schema
# (app/models/stage.py: Stage._schemas_declared).
_STAGE = {
    "id": "load",
    "description": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
    "signature": {
        "form": "replaces",
        "produces": [{"name": "doc_id", "type": "str", "nullable": False}],
    },
}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / "demo"
    compiled = pdir
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, _STAGE)
    workspace.set_projects_dir(tmp_path)
    return pdir


def test_versions_list_shows_each_version_and_its_message(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    page = client.get("/project/demo/workflow/versions")
    assert page.status_code == 200
    assert meta.version_id in page.text
    assert "v1" in page.text


def test_versions_list_offers_no_publishing_at_all(project: Path) -> None:
    project_service.save_working_copy_as_version(project.name, message="v1")
    page = client.get("/project/demo/workflow/versions")
    assert "/publish" not in page.text
    assert "<button type=\"submit\">Publish</button>" not in page.text


def test_run_of_a_project_with_no_version_says_there_is_none(project: Path) -> None:
    resp = client.post("/project/demo/run", follow_redirects=False)
    assert resp.status_code == 400
    assert "No version to run" in resp.json()["detail"]


def test_a_run_is_never_refused_for_the_state_of_its_version(project: Path) -> None:
    """The fixture's input authors no path, so the run stops there — never on the report."""
    project_service.save_working_copy_as_version(project.name, message="v1")
    resp = client.post("/project/demo/run", follow_redirects=False)
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "report" not in detail.lower()
    assert "no file bound" in detail


def test_the_publish_route_is_gone(project: Path) -> None:
    project_service.save_working_copy_as_version(project.name, message="v1")
    resp = client.post(
        "/project/demo/versions/not-a-version/publish", follow_redirects=False
    )
    assert resp.status_code == 404


def test_versions_route_redirects_to_workflow_versions(project: Path) -> None:
    r = client.get("/project/demo/versions", follow_redirects=False)
    assert r.status_code in (307, 308)
    assert r.headers["location"].endswith("/project/demo/workflow/versions")


def test_workflow_renders_the_editor(project: Path) -> None:
    # The working-copy editor lives at /workflow; it renders the graph + review controls.
    page = client.get("/project/demo/workflow")
    assert page.status_code == 200
    assert "Run workflow" in page.text or "Regenerate" in page.text


def test_workflow_versions_list_rows_link_to_version_detail(project: Path) -> None:
    meta = project_service.save_working_copy_as_version(project.name, message="v1")
    page = client.get("/project/demo/workflow/versions")
    assert f"/workflow/version/{meta.version_id}" in page.text
