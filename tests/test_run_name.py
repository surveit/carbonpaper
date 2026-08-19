from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import versioning, workspace
from app.services.project import save_working_copy_as_version
from run_seed import read_manifest
from stage_seed import add_stage

client = TestClient(app)


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(
        proj,
        {
            "id": "load",
            "description": "Load",
            "type": "input_data",
            "signature": {
                "form": "replaces",
                "produces": [
                    {"name": "name", "type": "str", "nullable": False},
                    {"name": "val", "type": "int", "nullable": False},
                ],
            },
            "connector": {
                "kind": "file",
                "params": {"path": str(data), "format": "csv"},
            },
        },
    )
    vid = save_working_copy_as_version(proj.name, message="seed", reviewer="test").version_id
    versioning.publish_version(proj.name, vid, reviewer="human")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background", lambda target, *args: target(*args))
    return proj


def _latest_run_id(project: Path) -> str:
    return sorted((project / "runs").iterdir())[-1].name


def test_posting_a_name_stores_it_on_the_manifest(project: Path) -> None:
    response = client.post(
        "/project/demo/run",
        data={"name": "Fundsies!"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    run_id = _latest_run_id(project)
    assert read_manifest(project, run_id)["name"] == "Fundsies!"


def test_the_new_run_form_puts_name_before_version_and_has_no_comment_field(project: Path) -> None:
    body = client.get("/project/demo/runs/new").text

    assert body.index('name="name"') < body.index('name="version_id"')
    assert 'name="comment"' not in body


def test_the_run_page_can_rename_and_clear_the_name(project: Path) -> None:
    client.post("/project/demo/run", data={"name": "Fundsies!"}, follow_redirects=False)
    run_id = _latest_run_id(project)

    renamed = client.post(
        f"/project/demo/runs/{run_id}/name",
        data={"name": "Renamed"},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    assert renamed.headers["location"] == f"/project/demo/runs/{run_id}"
    assert read_manifest(project, run_id)["name"] == "Renamed"

    cleared = client.post(
        f"/project/demo/runs/{run_id}/name",
        data={"name": "   "},
        follow_redirects=False,
    )
    assert cleared.status_code == 303
    assert cleared.headers["location"] == f"/project/demo/runs/{run_id}"
    assert "name" not in read_manifest(project, run_id)


def test_the_run_page_renders_inline_name_editing(project: Path) -> None:
    client.post("/project/demo/run", data={"name": "Fundsies!"}, follow_redirects=False)
    run_id = _latest_run_id(project)

    body = client.get(f"/project/demo/runs/{run_id}?edit_name=1").text

    assert "Fundsies!" in body
    assert 'action="/project/demo/runs/' in body
    assert 'name="name"' in body
    assert ">Save</button>" in body
    assert "Clearing the name removes it." in body
