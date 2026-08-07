"""Which panel a run's stage opens: `supplied` shows the rows handed to the run, a
stage with no record at all shows the frozen definition and says it never ran, and a
stage absent from the pinned version is still a 404."""
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
            "id": "load", "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(data_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _COLUMNS},
        },
        {
            "id": "classify", "description": "Classify", "type": "python_row_function",
            "inputs": [{"id": "load", "schema": {"columns": _COLUMNS}}],
            "function": {"kind": "inline",
                         "code": 'def transform(row):\n    return {**row, "label": "x"}\n'},
            "signature": {
                "form": "extends",
                "reads": [{"input": "load", "columns": _COLUMNS}],
                "adds": [{"name": "label", "type": "str", "nullable": True}],
            },
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


def test_a_supplied_input_stage_shows_the_rows_that_entered_the_run(project: Path):
    """Not executed — but the rows handed in are its output of record, so they show."""
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    manifest = json.loads(
        (project / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
    load = next(r for r in manifest["stage_records"] if r["stage_id"] == "load")
    assert load["status"] == "supplied"
    assert load["output_row_count"] == 2
    assert (project / "runs" / run_id / load["output_path"]).exists()
    assert load["supplied_by"]["origin"] == "source_file"
    assert load["supplied_by"]["path"].endswith("rows.csv")

    response = _panel(run_id, "load")
    assert response.status_code == 200
    body = response.text
    assert 'id="stage-supplied"' in body
    assert "Supplied to this run, not computed by it" in body
    assert "rows.csv" in body
    assert "Not executed in this run" not in body


def test_a_stage_neither_executed_nor_supplied_says_it_never_ran(project: Path):
    """Scoped to `load` alone, `classify` has no record — what that panel exists for."""
    run_id = run_workflow_test(
        PROJECT, limit=2, offset=0, stage_ids=["load"])["run_id"]
    manifest = json.loads(
        (project / "runs" / run_id / "manifest.json").read_text(encoding="utf-8"))
    assert [r["stage_id"] for r in manifest["stage_records"]] == ["load"]

    response = _panel(run_id, "classify")
    assert response.status_code == 200
    body = response.text
    assert "Not executed in this run" in body
    # The frozen definition is what the panel has to offer in place of output.
    assert "Classify" in body
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
