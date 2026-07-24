"""POST /project/{name}/run pins the run to the workflow version chosen in the
form, defaulting to the latest version when the form omits version_id."""
from __future__ import annotations

import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
import app.services.run as run_service
from app.services import workspace
from app.main import app
from app.services.versioning import create_version_from_disk, list_versions, publish_version

client = TestClient(app)


@pytest.fixture
def project_two_versions(tmp_path, monkeypatch):
    """A project with an input_data stage snapshotted into TWO versions. Both
    authored the same (existing) data file, so a run against either version is
    ready without any binding — the tests can isolate version selection."""
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    v1 = create_version_from_disk(proj, message="v1", reviewer="test")
    # version ids are second-resolution timestamps; without this the two versions
    # can land in the same wall-clock second and collide.
    time.sleep(1.1)
    v2 = create_version_from_disk(proj, message="v2", reviewer="test")
    # runs pin PUBLISHED versions, so publish both to isolate version SELECTION.
    publish_version(proj, v1.version_id, reviewer="test")
    publish_version(proj, v2.version_id, reviewer="test")
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_posted_version_id_pins_the_run(project_two_versions):
    """version_id from the form is the version the run executes — even when it is
    not the latest one."""
    older = list_versions(project_two_versions)[-1].version_id  # list is newest-first
    resp = client.post("/project/demo/run",
                       data={"version_id": older}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project_two_versions)["workflow_version"] == older


def test_omitted_version_id_defaults_to_latest(project_two_versions):
    """No version_id in the form → the run pins to the latest version."""
    latest = list_versions(project_two_versions)[0].version_id
    resp = client.post("/project/demo/run", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project_two_versions)["workflow_version"] == latest


def test_runs_page_renders_version_picker_latest_selected(project_two_versions):
    """The run form carries a version_id <select> listing every version, with the
    latest pre-selected — so a run defaults to latest but any version is one click
    away, in the same form that collects the input paths."""
    versions = list_versions(project_two_versions)  # newest-first
    latest, older = versions[0].version_id, versions[-1].version_id
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="version_id"' in resp.text
    assert latest in resp.text and older in resp.text
    assert f'value="{latest}" selected' in resp.text          # latest is the default
    assert 'name="binding__load"' in resp.text                # inputs share the form


def _seed_load_stage(proj):
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")


def test_run_picker_offers_only_published_versions(tmp_path, monkeypatch):
    """A run pins a PUBLISHED version, so the picker lists only published ones —
    an unpublished (agent-minted or not-yet-approved) version is never offered."""
    proj = tmp_path / "demo"
    _seed_load_stage(proj)
    published = create_version_from_disk(proj, message="approved", reviewer="test").version_id
    time.sleep(1.1)
    unpublished = create_version_from_disk(proj, message="draft", reviewer="test").version_id
    publish_version(proj, published, reviewer="test")  # only the older one
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)

    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert published in resp.text          # the published version IS offered
    assert unpublished not in resp.text    # the unpublished one is NOT
    assert 'name="version_id"' in resp.text


def test_run_form_hidden_when_no_published_version(tmp_path, monkeypatch):
    """A project whose only version is unpublished shows 'publish a version first'
    instead of a run form — nothing is runnable."""
    proj = tmp_path / "demo"
    _seed_load_stage(proj)
    create_version_from_disk(proj, message="unpublished", reviewer="test")  # never published
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)

    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="version_id"' not in resp.text   # no run form
    assert "publish a version" in resp.text


@pytest.fixture
def project_versions_diff_paths(tmp_path, monkeypatch):
    """Two versions whose input stage authors DIFFERENT data files (a.csv in v1,
    b.csv in v2/latest). Lets a test check that binding provenance is judged
    against the SELECTED version's authored path, not always the latest's."""
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    a, b = proj / "a.csv", proj / "b.csv"
    pd.DataFrame({"name": ["x"], "val": [1]}).to_csv(a, index=False)
    pd.DataFrame({"name": ["y"], "val": [2]}).to_csv(b, index=False)
    compiled = proj / "compiled" / "01_load.json"

    def _author(path):
        compiled.write_text(json.dumps(
            {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(path), "format": "csv"}}}),
            encoding="utf-8")

    _author(a)
    v1 = create_version_from_disk(proj, message="v1 reads a.csv", reviewer="test")
    time.sleep(1.1)
    _author(b)
    v2 = create_version_from_disk(proj, message="v2 reads b.csv", reviewer="test")
    # runs pin PUBLISHED versions.
    publish_version(proj, v1.version_id, reviewer="test")
    publish_version(proj, v2.version_id, reviewer="test")
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(workspace, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
                        lambda target, *args: target(*args))
    return proj


def test_binding_provenance_uses_the_selected_versions_authored_path(
    project_versions_diff_paths,
):
    """Posting the SELECTED (older) version's OWN authored path is not a run
    binding — provenance is 'workflow'. It must be judged against that version's
    authored path, not the latest version's (which authors a different file)."""
    proj = project_versions_diff_paths
    older = list_versions(proj)[-1].version_id  # v1, authored a.csv
    resp = client.post("/project/demo/run",
                       data={"version_id": older, "binding__load": str(proj / "a.csv")},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(proj)["input_bindings"]["load"]["source"] == "workflow"


def test_run_inputs_endpoint_returns_the_selected_versions_inputs(
    project_versions_diff_paths,
):
    """GET /run-inputs?version_id= returns that version's file inputs, so the run
    form can refresh its path fields when the version dropdown changes — each
    version reports its own authored path."""
    proj = project_versions_diff_paths
    versions = list_versions(proj)  # newest-first: v2 (b.csv), v1 (a.csv)
    latest, older = versions[0].version_id, versions[-1].version_id

    latest_inputs = client.get(f"/project/demo/run-inputs?version_id={latest}").json()
    assert latest_inputs == [{"stage_id": "load", "name": "Load",
                              "path": str(proj / "b.csv")}]
    older_inputs = client.get(f"/project/demo/run-inputs?version_id={older}").json()
    assert older_inputs[0]["path"] == str(proj / "a.csv")
