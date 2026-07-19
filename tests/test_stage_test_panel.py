"""The node review partial renders the stage's tests as a skimmable report."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

_IN_SCHEMA = {"columns": [{"name": "amount", "type": "float", "nullable": False}]}
_OUT_SCHEMA = {"columns": [
    {"name": "amount", "type": "float", "nullable": False},
    {"name": "doubled", "type": "float", "nullable": False},
]}


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "name": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "output_schema": _IN_SCHEMA,
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "name": "Double", "type": "python_row_function",
        "inputs": [{"id": "load", "schema": _IN_SCHEMA}],
        "output_schema": _OUT_SCHEMA,
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
    import app.web.loading as loading
    import app.web.routers.node_review as node_review_router
    monkeypatch.setattr(node_review_router, "EXAMPLES_DIR", tmp_path)
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return TestClient(app)


def test_panel_shows_each_test_with_status(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/double/review-partial")
    assert response.status_code == 200
    html = response.text
    assert "Tests" in html
    assert "doubles_two" in html and "The basic doubling contract." in html
    assert "expects_wrong_value" in html
    assert "passed" in html and "mismatch" in html


def test_panel_without_tests_has_no_tests_section(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/load/review-partial")
    assert response.status_code == 200
    assert "test-report" not in response.text
