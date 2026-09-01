"""POST /project/{name}/run pins the run to the workflow version chosen in the
form, defaulting to the latest version when the form omits version_id."""
from __future__ import annotations


import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.services import workspace
from app.main import app
from app.services.project import save_working_copy_as_version
from app.services.versioning import (
    create_version_from_stages,
    list_versions,
)
from stage_seed import add_stage, set_stages
from run_seed import read_manifest

client = TestClient(app)

# The columns of the CSVs these fixtures write; Stage._schemas_declared wants them.
_ROWS_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": False},
                            {"name": "val", "type": "int", "nullable": False}]}


@pytest.fixture
def project_two_versions(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    add_stage(proj, stage)
    save_working_copy_as_version(proj.name, message="v1")
    save_working_copy_as_version(proj.name, message="v2")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return read_manifest(run_dir.parent.parent, run_dir.name)


def test_posted_version_id_pins_the_run_even_when_it_is_not_the_latest(project_two_versions):
    older = list_versions(project_two_versions.name)[-1].version_id  # list is newest-first
    resp = client.post("/project/demo/run",
                       data={"version_id": older}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project_two_versions)["workflow_version"] == older


def test_omitted_version_id_defaults_to_latest(project_two_versions):
    latest = list_versions(project_two_versions.name)[0].version_id
    resp = client.post("/project/demo/run", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project_two_versions)["workflow_version"] == latest


def test_new_run_page_renders_version_picker_latest_selected(project_two_versions):
    versions = list_versions(project_two_versions.name)  # newest-first
    # The picker sits in the same form that collects the input paths, because a
    # different version can author different input stages.
    latest, older = versions[0].version_id, versions[-1].version_id
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="version_id"' in resp.text
    assert latest in resp.text and older in resp.text
    assert f'value="{latest}" selected' in resp.text          # latest is the default
    assert 'name="binding__load"' in resp.text                # inputs share the form


def test_new_run_page_opens_on_the_version_the_link_named(project_two_versions):
    versions = list_versions(project_two_versions.name)  # newest-first
    latest, older = versions[0].version_id, versions[-1].version_id

    resp = client.get(f"/project/demo/runs/new?version_id={older}")

    assert resp.status_code == 200
    assert f'value="{older}" selected' in resp.text
    assert f'value="{latest}" selected' not in resp.text


def test_new_run_page_404s_for_a_version_id_no_version_carries(project_two_versions):
    resp = client.get("/project/demo/runs/new?version_id=20990101T000000")
    # Opening on the latest instead would launch a workflow other than the one named.
    assert resp.status_code == 404


def _seed_load_stage(proj):
    proj.mkdir(parents=True, exist_ok=True)
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(data, index=False)
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    add_stage(proj, stage)


def test_run_picker_offers_every_stored_version(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    _seed_load_stage(proj)
    older = save_working_copy_as_version(proj.name, message="approved").version_id
    newer = save_working_copy_as_version(proj.name, message="draft").version_id
    workspace.set_projects_dir(tmp_path)

    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert older in resp.text
    assert newer in resp.text
    assert f'value="{newer}" selected' in resp.text  # the newest is preselected
    assert 'name="version_id"' in resp.text


def test_run_form_shown_when_the_only_version_is_unpublished(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    _seed_load_stage(proj)
    vid = save_working_copy_as_version(proj.name, message="unpublished").version_id
    workspace.set_projects_dir(tmp_path)

    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="version_id"' in resp.text
    assert vid in resp.text
    assert "No version to run" not in resp.text


def test_run_form_hidden_when_the_project_has_no_version(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    _seed_load_stage(proj)
    workspace.set_projects_dir(tmp_path)

    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="version_id"' not in resp.text   # no run form
    # The zero state names what is missing and offers the one action that fixes it.
    assert "No version to run" in resp.text
    assert 'href="/project/demo/workflow" class="btn primary"' in resp.text


@pytest.fixture
def project_versions_diff_paths(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    a, b = proj / "a.csv", proj / "b.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(a, index=False)
    pd.DataFrame({"name": ["y"], "val": [2]}).to_csv(b, index=False)
    def _author(path):
        set_stages(proj, [
            {"id": "load", "description": "Load", "type": "input_data",
             "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
             "connector": {"kind": "file",
                           "params": {"path": str(path), "format": "csv"}}}])

    _author(a)
    save_working_copy_as_version(proj.name, message="v1 reads a.csv")
    _author(b)
    save_working_copy_as_version(proj.name, message="v2 reads b.csv")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def test_posting_the_selected_versions_own_authored_path_is_not_a_binding(
    project_versions_diff_paths,
):
    proj = project_versions_diff_paths
    older = list_versions(proj.name)[-1].version_id  # v1, authored a.csv
    resp = client.post("/project/demo/run",
                       data={"version_id": older, "binding__load": ""},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(proj)["input_bindings"]["load"]["source"] == "workflow"


def test_new_run_page_binds_the_named_versions_own_input_paths(
    project_versions_diff_paths,
):
    proj = project_versions_diff_paths
    older = list_versions(proj.name)[-1].version_id  # v1, authored a.csv

    resp = client.get(f"/project/demo/runs/new?version_id={older}")

    # The authored path is the picker's blank option, so the page names v1's file
    # and not v2's — the fields describe whichever version is selected.
    authored, other = str(proj / "a.csv"), str(proj / "b.csv")
    assert authored in resp.text
    assert other not in resp.text


def test_run_inputs_endpoint_returns_the_selected_versions_inputs(
    project_versions_diff_paths,
):
    proj = project_versions_diff_paths
    versions = list_versions(proj.name)  # newest-first: v2 (b.csv), v1 (a.csv)
    latest, older = versions[0].version_id, versions[-1].version_id

    latest = client.get(f"/project/demo/run-inputs?version_id={latest}").json()
    assert latest["inputs"] == [
        {"stage_id": "load", "authored_path": str(proj / "b.csv"),
         "selected_file_ids": [], "limit": None}]
    older = client.get(f"/project/demo/run-inputs?version_id={older}").json()
    assert older["inputs"][0]["authored_path"] == str(proj / "a.csv")
    # The files are project-wide, so both answers carry the same list.
    assert latest["files"] == older["files"]


def _store_version_without_working_copy(tmp_path) -> str:
    """A version written straight to the store, the way a rebuild from outside does."""
    proj = tmp_path / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(data, index=False)
    workspace.set_projects_dir(tmp_path)
    return create_version_from_stages(
        proj.name,
        [{"id": "load", "description": "Load", "type": "input_data",
          "signature": {"form": "replaces", "produces": _ROWS_SCHEMA["columns"]},
          "connector": {"kind": "file",
                        "params": {"path": str(data), "format": "csv"}}}],
        message="rebuilt elsewhere",
    ).version_id


def test_run_form_shown_for_a_version_stored_without_a_working_copy(tmp_path):
    vid = _store_version_without_working_copy(tmp_path)

    resp = client.get(f"/project/demo/runs/new?version_id={vid}")

    assert resp.status_code == 200
    assert 'name="version_id"' in resp.text
    assert f'value="{vid}" selected' in resp.text
    assert 'name="binding__load"' in resp.text
    assert "No workflow to run" not in resp.text


def test_runs_page_offers_a_new_run_without_a_working_copy(tmp_path):
    _store_version_without_working_copy(tmp_path)

    resp = client.get("/project/demo/runs")

    assert resp.status_code == 200
    assert 'href="/project/demo/runs/new"' in resp.text
