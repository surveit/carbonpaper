"""Duplicating a run: GET /project/{p}/runs/new?from_run={id} opens the run form on
that run's version, file picks and row limits, and says what it could not copy. The
form is not submitted here — a duplicate launches nothing until the reader does.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.runtime.manifest import read_run_manifest
from app.services import versioning, workspace
from app.services.project import save_working_copy_as_version
from app.services.uploads import save_upload
from app.web.run_inputs import build_run_input_choices
from run_seed import store_manifest
from stage_seed import add_stage

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch) -> Path:
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(proj / "a.csv", index=False)
    add_stage(proj, {
        "id": "load", "description": "Load", "type": "input_data",
        "signature": {"form": "replaces", "produces": [
            {"name": "name", "type": "str", "nullable": False},
            {"name": "val", "type": "int", "nullable": False}]},
        "connector": {"kind": "file",
                      "params": {"path": str(proj / "a.csv"), "format": "csv"}}})
    version_id = save_working_copy_as_version(
        proj.name, message="seed", reviewer="test").version_id
    versioning.publish_version(proj.name, version_id, reviewer="human")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _store(name: str, frame: pd.DataFrame, tmp_path: Path) -> str:
    path = tmp_path / name
    frame.to_csv(path, index=False)
    with path.open("rb") as handle:
        return save_upload(name, handle, "demo").sha256


def _squash(text: str) -> str:
    """The note wraps across source lines; its sentences do not."""
    return re.sub(r"\s+", " ", text)


def _pick_options(page_text: str) -> str:
    return page_text.split('name="binding__load"', 1)[1].split("</select>", 1)[0]


def _run_id(project: Path) -> str:
    return sorted((project / "runs").iterdir())[-1].name


def test_duplicate_opens_on_the_copied_runs_file_and_row_limit(project, tmp_path):
    sha = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": sha, "limit__load": "3"},
                follow_redirects=False)
    run_id = _run_id(project)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert page.status_code == 200
    assert f'<option value="{sha}" selected' in page.text
    assert 'placeholder="all"\n             value="3"' in page.text
    assert "1 file pick and 1 row limit" in _squash(page.text)
    assert f'value="{read_run_manifest("demo", run_id).workflow_version}" selected' in page.text


def test_duplicate_launches_nothing(project, tmp_path):
    client.post("/project/demo/run", data={"binding__load": ""}, follow_redirects=False)
    before = sorted(p.name for p in (project / "runs").iterdir())

    client.get(f"/project/demo/runs/new?from_run={before[-1]}")

    assert sorted(p.name for p in (project / "runs").iterdir()) == before


def test_duplicate_names_a_file_the_project_no_longer_holds(project, tmp_path):
    sha = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": sha}, follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    record["parameters"]["run_bindings"]["load"] = {"path": "/gone/b.csv", "format": "csv"}
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert "0 file picks" in _squash(page.text)
    assert "could not be copied" in _squash(page.text)
    assert f'<option value="{sha}" selected' not in page.text


def test_duplicate_names_a_row_cap_this_form_cannot_carry(project):
    """The form caps file inputs; a hidden field would cap what the reader cannot see."""
    client.post("/project/demo/run", data={"binding__load": ""}, follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    record["parameters"]["limits"] = {"classify": 25}
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert "0 row limits" in _squash(page.text)
    assert "caps file inputs only" in _squash(page.text)
    assert "<code>classify</code> at 25" in _squash(page.text)
    assert 'name="limit__classify"' not in page.text


def test_duplicating_a_test_run_says_this_one_is_a_production_run(project):
    client.post("/project/demo/run", data={"binding__load": ""}, follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    record["parameters"] |= {"is_test_run": True, "queue_auto_approve": True}
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert "That was a test run" in _squash(page.text)


def test_duplicate_carries_the_cache_setting_and_opens_the_fold_on_it(project):
    client.post("/project/demo/run", data={"binding__load": "", "bust_cache": "on"},
                follow_redirects=False)

    page = client.get(f"/project/demo/runs/new?from_run={_run_id(project)}")

    assert 'name="bust_cache" value="on"\n             checked' in page.text
    assert '<details class="run-advanced" open>' in page.text


def test_duplicate_of_a_missing_run_404s(project):
    assert client.get("/project/demo/runs/new?from_run=no-such-run").status_code == 404


def test_a_plain_new_run_page_copies_nothing(project, tmp_path):
    _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)

    page = client.get("/project/demo/runs/new")

    assert page.status_code == 200
    assert "Copied from run" not in page.text
    assert "selected" not in _pick_options(page.text)
    assert build_run_input_choices("demo").copied is None

