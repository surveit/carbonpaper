"""POST /project/{p}/version refuses to snapshot while any stage test is red."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}


def _seed_project(root: Path, expected_doubled: float) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"},
        "output_schema": _IN_SCHEMA,
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
        "function": {
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
        "tests": [{
            "name": "doubles_two",
            "inputs": {"load": [{"amount": 2.0}]},
            "expected": [{"amount": 2.0, "doubled": expected_doubled}],
        }],
    }), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    # The routers read the projects root live; point it once
    # module that imported it at the temp root.
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_red_test_blocks_version(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path, expected_doubled=5.0)  # wrong on purpose
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 400
    body = response.json()
    assert body["ok"] is False
    assert any("doubles_two" in issue for issue in body["issues"])
    assert not (tmp_path / "alpha" / "versions").exists()


def test_green_tests_allow_version(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path, expected_doubled=4.0)
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_project_without_workflow_still_gets_400(client: TestClient, tmp_path: Path) -> None:
    (tmp_path / "alpha").mkdir()
    response = client.post("/project/alpha/version", data={"message": "v1"})
    assert response.status_code == 400
