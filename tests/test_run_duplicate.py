"""?from_run= opens the run form on the settings a recorded run used."""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.core.files import ProjectFile, save_upload
from app.main import app
from app.runtime.manifest import read_run_manifest
from app.services import workspace
from app.services.project import save_working_copy_as_version
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
    save_working_copy_as_version(
        proj.name, message="seed").version_id
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setenv("CARBON_PAPER_FILES_ROOT", str(tmp_path / "files"))
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _store(name: str, frame: pd.DataFrame, tmp_path: Path) -> str:
    path = tmp_path / name
    frame.to_csv(path, index=False)
    with path.open("rb") as handle:
        return save_upload(name, handle, "demo").id


def _run_id(project: Path) -> str:
    return sorted((project / "runs").iterdir())[-1].name


def _pick_options(page_text: str) -> str:
    return page_text.split('name="binding__load"', 1)[1].split("</select>", 1)[0]


def _field_names(page_text: str) -> list[str]:
    return re.findall(r'<(?:input|select)[^>]*\bname="([^"]+)"', page_text)


def test_duplicate_opens_on_the_copied_runs_file_and_row_limit(project, tmp_path):
    file_id = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": file_id, "limit__load": "3"},
                follow_redirects=False)
    run_id = _run_id(project)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert page.status_code == 200
    assert f'<option value="{file_id}" selected' in page.text
    assert 'placeholder="all"\n             value="3"' in page.text
    assert f'value="{read_run_manifest("demo", run_id).workflow_version}" selected' in page.text


def test_duplicate_launches_nothing(project):
    client.post("/project/demo/run", data={"binding__load": ""}, follow_redirects=False)
    before = sorted(p.name for p in (project / "runs").iterdir())

    client.get(f"/project/demo/runs/new?from_run={before[-1]}")

    assert sorted(p.name for p in (project / "runs").iterdir()) == before


def test_duplicate_carries_a_row_cap_no_input_row_holds(project):
    """The form's own fields cap file inputs; every other cap rides hidden."""
    client.post("/project/demo/run", data={"binding__load": ""}, follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    record["parameters"]["limits"] = {"classify": 25}
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert ('<input type="hidden" class="js-carried-limit" name="limit__classify" '
            'value="25">') in page.text
    assert build_run_input_choices(
        "demo", None, read_run_manifest("demo", run_id)).carried_limits == {"classify": 25}


def test_a_cap_an_input_row_holds_is_not_also_carried_hidden(project):
    client.post("/project/demo/run", data={"binding__load": "", "limit__load": "7"},
                follow_redirects=False)

    page = client.get(f"/project/demo/runs/new?from_run={_run_id(project)}")

    assert 'class="js-carried-limit" name="limit__load"' not in page.text
    assert page.text.count('name="limit__load"') == 1


def test_duplicate_carries_the_cache_setting_and_opens_the_fold_on_it(project):
    client.post("/project/demo/run", data={"binding__load": "", "bust_cache": "on"},
                follow_redirects=False)

    page = client.get(f"/project/demo/runs/new?from_run={_run_id(project)}")

    assert 'name="bust_cache" value="on"\n             checked' in page.text
    assert '<details class="run-advanced" open>' in page.text


def test_a_file_this_project_no_longer_holds_leaves_the_row_on_its_authored_path(
    project, tmp_path,
):
    """Nothing to select, so the row is the plain one: blank IS the authored path."""
    file_id = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": file_id},
                follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    record["parameters"]["run_bindings"]["load"] = {"path": "/gone/b.csv", "format": "csv"}
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert "selected" not in _pick_options(page.text)
    assert str(project / "a.csv") in page.text


def test_duplicate_of_a_missing_run_404s(project):
    assert client.get("/project/demo/runs/new?from_run=no-such-run").status_code == 404


def test_a_plain_new_run_page_carries_nothing(project, tmp_path):
    _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)

    page = client.get("/project/demo/runs/new")

    assert page.status_code == 200
    assert "selected" not in _pick_options(page.text)
    assert "js-carried-limit" not in page.text
    assert build_run_input_choices("demo").carried_limits == {}


def test_the_form_a_duplicate_fills_in_is_the_form_it_always_was(project, tmp_path):
    """Same fields in the same order: a duplicate sets values, it adds no furniture."""
    file_id = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": file_id},
                follow_redirects=False)

    plain = client.get("/project/demo/runs/new").text
    copy = client.get(f"/project/demo/runs/new?from_run={_run_id(project)}").text

    assert _field_names(plain) == _field_names(copy)


def test_a_binding_recorded_under_the_old_sha256_directory_still_matches(
    project, tmp_path,
):
    """The store keyed a file's directory by sha256 before it keyed it by record id."""
    file_id = _store("b.csv", pd.DataFrame({"name": ["z"], "val": [9]}), tmp_path)
    client.post("/project/demo/run", data={"binding__load": file_id},
                follow_redirects=False)
    run_id = _run_id(project)
    record = read_run_manifest("demo", run_id).to_dict()
    stored = ProjectFile.load(file_id)
    record["parameters"]["run_bindings"]["load"]["path"] = str(
        tmp_path / "files" / stored.sha256 / stored.filename)
    store_manifest(project, run_id, record)

    page = client.get(f"/project/demo/runs/new?from_run={run_id}")

    assert f'<option value="{file_id}" selected' in page.text


def test_duplicate_carries_the_runs_name(project):
    client.post("/project/demo/run", data={"binding__load": "", "name": "Q3 sweep"},
                follow_redirects=False)

    page = client.get(f"/project/demo/runs/new?from_run={_run_id(project)}")

    assert '<input type="text" name="name" maxlength="200" autocomplete="off"\n' \
           '           value="Q3 sweep">' in page.text


def test_a_plain_new_run_page_has_an_empty_name(project):
    page = client.get("/project/demo/runs/new")

    assert 'name="name" maxlength="200" autocomplete="off"\n           value="">' in page.text
