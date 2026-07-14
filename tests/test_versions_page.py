"""Route tests for the versions page: unpublished versions show a publish action,
publishing stamps the meta and redirects, and the run trigger explains the
published gate."""
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
    "connector": {"kind": "computed_static"},
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


def test_versions_page_shows_publish_action_for_unpublished(project: Path) -> None:
    versioning.create_version(project, message="v1", reviewer="local")
    page = client.get("/project/demo/versions")
    assert page.status_code == 200
    assert "unpublished" in page.text
    assert "/publish" in page.text


def test_publish_route_stamps_and_redirects(project: Path) -> None:
    meta = versioning.create_version(project, message="v1", reviewer="local")
    resp = client.post(
        f"/project/demo/versions/{meta['id']}/publish", follow_redirects=False
    )
    assert resp.status_code == 303
    assert versioning.version_is_published(
        versioning.load_version_meta(project, meta["id"])
    )
    page = client.get("/project/demo/versions")
    assert "unpublished" not in page.text


def test_run_of_unpublished_project_explains_publish_gate(project: Path) -> None:
    versioning.create_version(project, message="v1", reviewer="local")
    resp = client.post("/project/demo/run", follow_redirects=False)
    assert resp.status_code == 400
    assert "publish" in resp.json()["detail"]
