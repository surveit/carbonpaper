"""POST /project/{name}/run with input-binding form fields, and the runs page
rendering one path field per file-kind input stage."""
from __future__ import annotations

import json
import time

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.routers.runs as runs_router
from app.core.models.records.workflow_run import WorkflowRun
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


def _manifest(proj) -> WorkflowRun:
    run_dir = sorted((proj / "runs").iterdir())[-1]
    return WorkflowRun.load(f"{proj.name}/{run_dir.name}")


def test_changed_field_becomes_run_binding(project, tmp_path):
    other = tmp_path / "b.csv"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_csv(other, index=False)
    resp = client.post("/project/demo/run",
                       data={"binding__load": str(other)}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project).input_bindings["load"]["source"] == "run"


def test_untouched_prefill_stays_workflow_source(project):
    authored = str(project / "a.csv")
    resp = client.post("/project/demo/run",
                       data={"binding__load": authored}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project).input_bindings["load"]["source"] == "workflow"


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
# fails LOUDLY on every read — listing included — as WorkflowLoadError. No page
# renders as if the store were healthy; the remedy for legacy documents is a
# store migration, never a silent skip. The trigger endpoint translates the
# failure into a structured 400 naming the issues.

def test_runs_page_fails_loudly_for_an_invalid_version(project):
    from app.services.loader import WorkflowLoadError

    _corrupt_version_document_with_relative_path(project)
    with pytest.raises(WorkflowLoadError, match="ABSOLUTE"):
        client.get("/project/demo/runs")


def test_trigger_run_returns_400_with_issues_for_an_invalid_version(project):
    _corrupt_version_document_with_relative_path(project)
    resp = client.post("/project/demo/run", data={}, follow_redirects=False)
    assert resp.status_code == 400
    assert any("ABSOLUTE" in issue for issue in resp.json()["issues"])


def test_loading_an_invalid_version_explicitly_surfaces_issues(project):
    from app.services.loader import WorkflowLoadError
    from app.services.versioning import load_version_stages

    version_id = _corrupt_version_document_with_relative_path(project)
    with pytest.raises(WorkflowLoadError) as exc:
        load_version_stages(project, version_id)
    assert any("ABSOLUTE" in issue for issue in exc.value.issues)


# ─── Resume: the run-existence guard reads the store, not a manifest.json file ──

def test_resume_route_does_not_404_for_a_real_run(project):
    """Regression: the resume route's pre-check used to stat run_dir/manifest.json
    directly, bypassing the document store the manifest now lives in — every real
    run would incorrectly 404 on resume. It must accept a run that genuinely
    exists (a completed run resumes as a no-op re-run of its already-ok stages)."""
    trigger = client.post("/project/demo/run",
                          data={"binding__load": str(project / "a.csv")},
                          follow_redirects=False)
    assert trigger.status_code == 303
    run_id = sorted((project / "runs").iterdir())[-1].name

    resp = client.post(f"/project/demo/runs/{run_id}/resume", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == f"/project/demo/runs/{run_id}"


def test_resume_route_404s_for_an_unknown_run(project):
    resp = client.post("/project/demo/runs/no-such-run/resume", follow_redirects=False)
    assert resp.status_code == 404
