"""A workflow test runs the frontier and injects each input_data stage's rows, so
those stages have no manifest record — while the run's graph, drawn from the whole
pinned version, still shows them. Clicking one must open its frozen definition and
say it never ran, not the bare 404 the panel used to render; a stage absent from the
pinned version too is still a 404."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.main import app
from app.runtime.runner import execute_run
from app.services import project as project_service
from app.services.workflow_test import run_workflow_test
from conftest import pinned_stages
from stage_seed import add_stage
from run_seed import read_manifest

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
            "inputs": [{"id": "load"}],
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
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    for index, stage in enumerate(_stages(data), start=1):
        add_stage(pdir, stage)
    workspace.set_projects_dir(tmp_path)
    project_service.save_working_copy_as_version(pdir.name, message="v1")
    return pdir


def _panel(run_id: str, stage_id: str):
    return client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/partial")


def test_input_stage_of_a_workflow_test_opens_instead_of_404ing(project: Path):
    run_id = run_workflow_test(PROJECT, limit=2, offset=0)["run_id"]
    manifest = read_manifest(project, run_id)
    assert [r["stage_id"] for r in manifest["stage_records"]] == ["classify"]

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
    run_id = str(execute_run(project / "runs", project.name, *pinned_stages(project))["run_id"])

    response = _panel(run_id, "load")
    assert response.status_code == 200
    assert "Not executed in this run" not in response.text
    assert "output data" in response.text  # the run's own output preview
