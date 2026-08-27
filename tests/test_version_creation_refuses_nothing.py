"""POST /project/{p}/version snapshots what the author has, whatever it is owed.

The compiler report tells them what is wrong; it does not hold the button. A working
copy that does not LOAD is the one refusal, and test_version_route_invalid_workflow
covers that.
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace
from stage_seed import add_stage

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_SUMMARY = "Doubles the amount on every row."


def _seed_project(root: Path, expected_doubled: float, summary: str | None = _SUMMARY) -> None:
    compiled = root / "alpha"
    compiled.mkdir(parents=True, exist_ok=True)
    add_stage(compiled, {
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    })
    function = {"kind": "inline",
                "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"}
    if summary is not None:
        function["summary"] = summary
    add_stage(compiled, {
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": function,
        "tests": [{
            "name": "doubles_two",
            "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": expected_doubled}],
        }],
    })


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    # The routers read the projects root live; point it once
    # module that imported it at the temp root.
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_a_mismatched_example_does_not_block_the_version(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path, expected_doubled=5.0)  # the step returns 4.0
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_an_undescribed_stage_does_not_block_the_version(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path, expected_doubled=4.0, summary=None)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    # The live-LLM journey wrote a summary-less report, then could not pin its version.
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_a_clean_workflow_versions(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path, expected_doubled=4.0)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_project_without_workflow_still_gets_400(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir(parents=True, exist_ok=True)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 400
