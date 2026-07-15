"""Route tests for the read-only version-detail page and run-this-version."""
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
    "id": "load", "name": "Load rows", "type": "input_data",
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


def test_version_detail_renders_frozen_graph_and_publish(project: Path) -> None:
    meta = versioning.create_version(project, message="v1", reviewer="local")
    page = client.get(f"/project/demo/workflow/version/{meta['id']}")
    assert page.status_code == 200
    assert meta["id"] in page.text
    assert "mermaid" in page.text          # the graph rendered
    assert "/publish" in page.text          # unpublished → Publish control present


def test_version_detail_404_for_unknown_version(project: Path) -> None:
    assert client.get("/project/demo/workflow/version/20990101T000000").status_code == 404


def test_run_this_version_404_for_nonexistent_version(project: Path) -> None:
    resp = client.post(
        "/project/demo/workflow/version/20990101T000000/run", follow_redirects=False
    )
    assert resp.status_code == 404


def test_run_this_version_gated_on_published(project: Path) -> None:
    meta = versioning.create_version(project, message="v1", reviewer="local")
    vid = meta["id"]
    # Unpublished → 400 explaining the publish gate.
    unpub = client.post(f"/project/demo/workflow/version/{vid}/run", follow_redirects=False)
    assert unpub.status_code == 400
    assert "publish" in unpub.json()["detail"]
    # Published → 303 to the run page.
    versioning.publish_version(project, vid, reviewer="local")
    pub = client.post(f"/project/demo/workflow/version/{vid}/run", follow_redirects=False)
    assert pub.status_code == 303
    assert "/runs/" in pub.headers["location"]
