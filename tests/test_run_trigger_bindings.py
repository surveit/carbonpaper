"""POST /project/{name}/run with input-binding form fields, and the runs page
rendering one path field per file-kind input stage."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services import versioning
from app.services import workspace
from app.services.project import save_working_copy_as_version

client = TestClient(app)


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "demo"
    (proj / "compiled").mkdir(parents=True)
    data = proj / "a.csv"
    pd.DataFrame({"name": ["x", "y"], "val": [1, 2]}).to_csv(data, index=False)
    # output_schema names the CSV's columns; every non-publish stage must declare
    # one (app/models/stage.py: Stage._schemas_declared).
    stage = {"id": "load", "description": "Load", "type": "input_data",
             "signature": {
                 "form": "replaces",
                 "produces": [
                     {"name": "name", "type": "str", "nullable": False},
                     {"name": "val", "type": "int", "nullable": False},
                 ],
             },
             "connector": {"kind": "file",
                           "params": {"path": str(data), "format": "csv"}}}
    (proj / "compiled" / "01_load.json").write_text(json.dumps(stage), encoding="utf-8")
    vid = save_working_copy_as_version(proj, message="seed", reviewer="test").version_id
    versioning.publish_version(proj, vid, reviewer="human")
    workspace.set_projects_dir(tmp_path)
    monkeypatch.setattr(run_service, "_run_in_background",
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


def test_binding_carries_the_bound_files_own_format(project, tmp_path):
    other = tmp_path / "b.parquet"
    pd.DataFrame({"name": ["z"], "val": [9]}).to_parquet(other, index=False)
    resp = client.post("/project/demo/run",
                       data={"binding__load": str(other)}, follow_redirects=False)
    assert resp.status_code == 303
    manifest = _manifest(project)
    assert manifest["parameters"]["run_bindings"]["load"]["format"] == "parquet"
    assert manifest["stage_records"][0]["status"] == "ok"


def test_binding_a_file_with_an_unreadable_extension_returns_400(project, tmp_path):
    other = tmp_path / "b.rtf"
    other.write_text("not a table", encoding="utf-8")
    resp = client.post("/project/demo/run",
                       data={"binding__load": str(other)}, follow_redirects=False)
    assert resp.status_code == 400
    assert ".rtf" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_untouched_prefill_stays_workflow_source(project):
    authored = str(project / "a.csv")
    resp = client.post("/project/demo/run",
                       data={"binding__load": authored}, follow_redirects=False)
    assert resp.status_code == 303
    assert _manifest(project)["input_bindings"]["load"]["source"] == "workflow"


def test_unbound_input_returns_400(project):
    compiled = project / "compiled" / "01_load.json"
    stage = json.loads(compiled.read_text(encoding="utf-8"))
    stage["connector"]["params"] = {}
    compiled.write_text(json.dumps(stage), encoding="utf-8")
    vid = save_working_copy_as_version(project, message="unbound", reviewer="test").version_id
    versioning.publish_version(project, vid, reviewer="human")

    resp = client.post("/project/demo/run",
                       data={"binding__load": ""}, follow_redirects=False)
    assert resp.status_code == 400
    assert "load" in resp.json()["detail"]
    assert not (project / "runs").exists()


def test_new_run_page_shows_one_field_per_file_input(project):
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'name="binding__load"' in resp.text
    assert str(project / "a.csv") in resp.text


# ─── The run form is its own page, not a block on the run history ────────────
# Configuring a run and reading the history are different tasks with different
# fields; "new" also has to reach run_new rather than being read as a run id by
# /runs/{run_id}, which is registered after it.

def test_runs_index_carries_no_run_form(project):
    resp = client.get("/project/demo/runs")
    assert resp.status_code == 200
    assert 'class="run-controls"' not in resp.text
    assert 'name="binding__load"' not in resp.text
    assert '/project/demo/runs/new' in resp.text  # the action that reaches it


def test_runs_index_carries_no_awaiting_review_banner(project):
    assert "banner-review" not in client.get("/project/demo/runs").text


def test_the_zero_state_offers_a_button_not_a_link_in_a_sentence(project):
    body = client.get("/project/demo/runs").text
    zero = body.split('class="empty-state"')[1].split("</div>")[0]
    assert "No runs yet" in zero
    assert '<a href="/project/demo/runs/new" class="btn primary">Start new run</a>' in zero


def test_new_is_the_run_form_not_a_run_id(project):
    resp = client.get("/project/demo/runs/new")
    assert resp.status_code == 200
    assert 'action="/project/demo/run"' in resp.text


def test_new_run_page_labels_the_row_cap_separately(project):
    resp = client.get("/project/demo/runs/new")
    # It used to sit inside the row's label, where clicking "first"/"rows" focused
    # the read-only path field — the label's first control.
    assert 'class="run-limit"' in resp.text
    assert 'for="binding__load"' in resp.text  # the name line labels the path field


def _corrupt_version_document_with_relative_path(project):
    from app.core.persistence import get_store
    from app.services.versioning import list_versions

    version_id = list_versions(project)[0].version_id
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
