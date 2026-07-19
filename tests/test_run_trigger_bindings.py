"""POST /project/{name}/run with input-binding form fields, and the runs page
rendering one path field per file-kind input stage."""
from __future__ import annotations

import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
from app.main import app
from app.services.versioning import create_version

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    stage = {"id": "load", "name": "Load", "type": "input_data",
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    create_version(proj, message="seed", reviewer="test")
    monkeypatch.setattr(runs_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(runs_router, "run_in_background",
                        lambda target, *args: target(*args))
    return proj


def _manifest(proj):
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_changed_field_becomes_run_binding(project, tmp_path):
    other = tmp_path / "b.csv"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_csv(other, index=False)
    resp = client.post("/project/demo/run",
                       data={"binding__load": str(other)}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project)["input_bindings"]["load"]["source"] == "run"


def test_untouched_prefill_stays_workflow_source(project):
    authored = str(project / "a.csv")
    resp = client.post("/project/demo/run",
                       data={"binding__load": authored}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project)["input_bindings"]["load"]["source"] == "workflow"


def test_unbound_input_returns_400(project):
    # Strip the authored path so the input is unbound, then post an empty field.
    compiled = project / "compiled" / "01_load.json"
    stage = json.loads(compiled.read_text(encoding="utf-8"))
    stage["connector"]["params"] = {}
    compiled.write_text(json.dumps(stage), encoding="utf-8")
    # version ids are second-resolution timestamps (versioning.create_version);
    # without this the fixture's version and this one can land in the same
    # wall-clock second and collide (FileExistsError), unrelated to what this
    # test is checking.
    time.sleep(1.1)
    create_version(project, message="unbound", reviewer="test")

    resp = client.post("/project/demo/run",
                       data={"binding__load": ""}, follow_redirects=False)
    assert resp.status_code == 400
    assert "load" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_runs_page_shows_one_field_per_file_input(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'name="binding__load"' in resp.text
    assert str(project / "a.csv") in resp.text


def _corrupt_version_document_with_relative_path(project):
    """Simulate a version document written before absolute paths were enforced:
    rewrite the stored WorkflowVersion RAW through the store (bypassing model
    validation, as an older writer effectively did) so the document no longer
    validates on read. Returns the corrupted version's id."""
    from app.core.persistence import get_store
    from app.services.versioning import list_versions

    version_id = list_versions(project)[0]["id"]
    store = get_store()
    doc = store.read("workflow_version", f"{project.name}/{version_id}")
    doc["stages"][0]["connector"]["params"]["path"] = "relative/a.csv"
    store.write("workflow_version", f"{project.name}/{version_id}", doc)
    return version_id


# A version document that no longer validates (e.g. a legacy repo-relative path)
# is SKIPPED by list_versions (the store's tolerant-listing semantics), so it can
# never be selected as "the latest" — a project whose only version is invalid
# behaves as version-less. Loading it EXPLICITLY (a pinned run / resume) must
# surface validation issues as WorkflowLoadError, never a raw pydantic 500.

def test_runs_page_treats_an_invalid_only_version_as_versionless_not_500(project):
    _corrupt_version_document_with_relative_path(project)
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    # No silently-passing binding form for a version that can't be selected.
    assert 'name="binding__load"' not in resp.text


def test_trigger_run_returns_400_when_the_only_version_is_invalid(project):
    _corrupt_version_document_with_relative_path(project)
    resp = client.post("/project/demo/run", data={}, follow_redirects=False)
    assert resp.status_code == 400
    assert "No version to run" in resp.json()["detail"]


def test_loading_an_invalid_version_explicitly_surfaces_issues(project):
    from app.services.loader import WorkflowLoadError
    from app.services.versioning import load_version_stages

    version_id = _corrupt_version_document_with_relative_path(project)
    with pytest.raises(WorkflowLoadError) as exc:
        load_version_stages(project, version_id)
    assert any("ABSOLUTE" in issue for issue in exc.value.issues)
