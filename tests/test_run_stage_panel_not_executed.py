"""A workflow test injects its input_data stages' rows rather than computing them,
but run_subset still writes that slice out and records it — so the input stage's
panel shows this run's own output, and the 1:1 stage below it can be read as a diff
against it. A stage the run holds NO record for still opens on its frozen definition
saying it never ran; a stage absent from the pinned version too is still a 404."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.main import app
from app.runtime.runner import execute_run
from app.services import versioning
from app.services import project as project_service
from app.services.workflow_test import run_workflow_test
from conftest import pinned_stages

client = TestClient(app)

PROJECT = "not_executed_panel"
_COLUMNS = [{"name": "name", "type": "str", "nullable": True}, {"name": "val", "type": "int", "nullable": True}]


def _stages(data_path: Path) -> list[dict]:
    return [
        {
            "id": "load", "name": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(data_path), "format": "csv"}},
            "output_schema": {"columns": _COLUMNS},
        },
        {
            "id": "classify", "name": "Classify", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
            "function": {"kind": "inline",
                         "code": 'def transform(row):\n    return {**row, "label": "x"}\n'},
            "output_schema": {
                "columns": [*_COLUMNS, {"name": "label", "type": "str", "nullable": True}]},
        },
    ]


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pdir = tmp_path / PROJECT
    (pdir / "compiled").mkdir(parents=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    for index, stage in enumerate(_stages(data), start=1):
        (pdir / "compiled" / f"{index:02d}_{stage['id']}.json").write_text(
            json.dumps(stage), encoding="utf-8")
    workspace.set_projects_dir(tmp_path)
    version_id = project_service.save_working_copy_as_version(
        pdir, message="v1", reviewer="test").version_id
    versioning.publish_version(pdir, version_id, reviewer="test")
    return pdir


def _panel(run_id: str, stage_id: str):
    return client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/partial")


def _manifest(project: Path, run_id: str) -> dict:
    return json.loads(
        (project / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))


def test_a_workflow_tests_input_stage_shows_the_slice_it_ran_on(project: Path):
    """The injected rows are this run's input output, not a hole in it."""
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    assert [r["stage_id"] for r in _manifest(project, run_id)["stage_records"]] == [
        "load", "classify"]

    response = _panel(run_id, "load")
    assert response.status_code == 200
    assert "Not executed in this run" not in response.text
    assert "output data" in response.text


def test_the_stage_below_an_input_now_reads_as_a_diff_against_it(project: Path):
    # The diff aligns a 1:1 stage's output with its input's STORED frame, so with
    # no input frame on disk there was nothing to align a workflow test's first
    # transform against and the pane fell back to the plain output view.
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    body = _panel(run_id, "classify").text
    assert 'class="preview-block stage-diff"' in body


def test_a_stage_the_run_holds_no_record_for_opens_instead_of_404ing(project: Path):
    """A run whose manifest never covered a stage the graph still draws."""
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    manifest = _manifest(project, run_id)
    manifest["stage_records"] = [
        r for r in manifest["stage_records"] if r["stage_id"] != "load"]
    (project / "runs" / run_id / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")

    response = _panel(run_id, "load")
    assert response.status_code == 200
    body = response.text
    assert "Not executed in this run" in body
    # The frozen definition is what the panel has to offer in place of output:
    # the stage's identity, its connector, and its declared output schema.
    assert "Load rows" in body
    assert "rows.csv" in body
    assert 'id="stage-not-executed"' in body


def test_a_stage_the_run_never_heard_of_is_still_a_404(project: Path):
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    assert _panel(run_id, "no_such_stage").status_code == 404


def test_a_production_runs_input_stage_still_shows_its_run_detail(project: Path):
    """The not-executed panel must not swallow the ordinary case: a production run
    DOES execute its input stage, so that panel keeps showing this run's output."""
    run_id = str(execute_run(project, project, *pinned_stages(project))["run_id"])

    response = _panel(run_id, "load")
    assert response.status_code == 200
    assert "Not executed in this run" not in response.text
    assert "output data" in response.text  # the run's own output preview
