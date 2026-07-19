"""Route tests for the versions LIST page: unpublished versions show a read-only
status (Publish lives only on the version-detail page), publishing stamps the meta
and redirects to that detail page, and the run trigger explains the published
gate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.web.config as web_config
import app.web.loading as loading
import app.web.routers.node_review as node_review_router
import app.web.routers.project as project_router
import app.web.routers.runs as runs_router
from app.main import app
from app.services import versioning

client = TestClient(app)

_STAGE = {
    "id": "load",
    "name": "Load rows",
    "type": "input_data",
    "connector": {"kind": "file"},
}


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / "demo"
    compiled = pdir / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps(_STAGE), encoding="utf-8")
    for mod in (web_config, loading, node_review_router, project_router, runs_router):
        monkeypatch.setattr(mod, "EXAMPLES_DIR", tmp_path, raising=False)
    return pdir


def test_versions_list_shows_read_only_unpublished_status(project: Path) -> None:
    """The LIST shows published state read-only — no Publish form/button. Publishing
    is an approval act gated behind having looked at the version, so the action lives
    only on the version-detail page."""
    versioning.create_version(project, message="v1", reviewer="local")
    page = client.get("/project/demo/workflow/versions")
    assert page.status_code == 200
    assert "unpublished" in page.text


def test_versions_list_never_contains_a_publish_form(project: Path) -> None:
    versioning.create_version(project, message="v1", reviewer="local")
    page = client.get("/project/demo/workflow/versions")
    assert "/publish" not in page.text
    assert "<button type=\"submit\">Publish</button>" not in page.text


def test_publish_route_stamps_and_redirects_to_detail(project: Path) -> None:
    """Publish now redirects to the version's own detail page (you land back on the
    version you just approved), not the list."""
    meta = versioning.create_version(project, message="v1", reviewer="local")
    resp = client.post(
        f"/project/demo/versions/{meta['id']}/publish", follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].endswith(f"/project/demo/workflow/version/{meta['id']}")
    assert versioning.load_version_meta(project, meta["id"])["published"]
    page = client.get("/project/demo/workflow/versions")
    assert "unpublished" not in page.text


def test_run_of_unpublished_project_explains_publish_gate(project: Path) -> None:
    versioning.create_version(project, message="v1", reviewer="local")
    resp = client.post("/project/demo/run", follow_redirects=False)
    assert resp.status_code == 400
    assert "publish" in resp.json()["detail"]


def test_publish_route_rejects_non_timestamp_version_id(project: Path) -> None:
    before = versioning.list_versions(project)
    resp = client.post(
        "/project/demo/versions/not-a-version/publish", follow_redirects=False
    )
    assert resp.status_code == 404
    assert "not-a-version" in resp.json()["detail"]
    assert versioning.list_versions(project) == before


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
    meta = versioning.create_version(project, message="v1", reviewer="local")
    page = client.get("/project/demo/workflow/versions")
    assert f"/workflow/version/{meta['id']}" in page.text
