"""The node review partial renders the stage's tests as a skimmable report."""
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


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": _IN_SCHEMA["columns"]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
        "tests": [
            {"name": "doubles_two", "description": "The basic doubling contract.",
             "inputs": {"load": [{"amount": 2.0}]},
             "expected": [{"amount": 2.0, "doubled": 4.0}]},
            {"name": "expects_wrong_value",
             "inputs": {"load": [{"amount": 3.0}]},
             "expected": [{"amount": 3.0, "doubled": 7.0}]},
        ],
    }), encoding="utf-8")


@pytest.fixture
def client(tmp_path: Path, monkeypatch) -> TestClient:
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_panel_shows_each_test_with_status(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/double/panel")
    assert response.status_code == 200
    html = response.text
    assert "Tests" in html
    assert "doubles_two" in html and "The basic doubling contract." in html
    assert "expects_wrong_value" in html
    assert "passed" in html and "mismatch" in html


def test_expected_output_marks_the_columns_the_step_adds(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # `doubled` is in the output schema and in no input's; `amount` is carried in.
    assert '<th class="test-col-new">doubled</th>' in html
    assert "<th>amount</th>" in html
    assert '<td class="test-col-new">4.0</td>' in html
    assert "<code>doubled</code>" in html  # named in the caption, not colour alone


def test_panel_without_tests_has_no_tests_section(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/load/panel")
    assert response.status_code == 200
    assert "test-report" not in response.text
