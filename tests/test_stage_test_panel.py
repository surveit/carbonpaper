"""The node review partial renders the stage's tests as a skimmable report."""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import workspace

_AMOUNT = {"name": "amount", "type": "float", "nullable": False}
# `note` is carried through: in the input schema, read by nothing, written by nothing.
_IN_SCHEMA = {"columns": [_AMOUNT, {"name": "note", "type": "str", "nullable": False}]}


def _seed_project(root: Path) -> None:
    compiled = root / "alpha" / "compiled"
    compiled.mkdir(parents=True)
    (compiled / "01_load.json").write_text(json.dumps({
        "id": "load", "description": "Load", "type": "input_data",
        "connector": {"kind": "file"}, "signature": {"form": "replaces", "produces": _IN_SCHEMA["columns"]},
    }), encoding="utf-8")
    (compiled / "02_double.json").write_text(json.dumps({
        "id": "double", "description": "Double", "type": "python_row_function",
        "inputs": [{"id": "load"}],
        "signature": {
            "form": "extends",
            "reads": [{"input": "load", "columns": [_AMOUNT]}],
            "adds": [{"name": "doubled", "type": "float", "nullable": False}],
        },
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return {**row, 'doubled': row['amount'] * 2}\n"},
        # The rows state their columns in neither schema's order, so the table's order
        # can only come from the schema.
        "tests": [
            {"name": "doubles_two", "description": "The basic doubling contract.",
             "inputs": {"load": [{"note": "opening balance", "amount": 2.0}]},
             "expected": [{"doubled": 4.0, "note": "opening balance", "amount": 2.0}]},
            {"name": "expects_wrong_value",
             "inputs": {"load": [{"note": "closing balance", "amount": 3.0}]},
             "expected": [{"doubled": 7.0, "note": "closing balance", "amount": 3.0}]},
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


def test_expected_output_marks_the_columns_the_step_writes(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # `doubled` is what the signature writes; `amount` and `note` are carried through.
    assert '<th class="test-col-written">doubled</th>' in html
    assert '<td class="test-col-written">4.0</td>' in html
    assert "<th>amount</th>" in html and "<td>opening balance</td>" in html
    assert "<code>doubled</code>" in html  # named in the caption, not colour alone


def test_a_carried_through_column_sits_where_the_schema_puts_it(
    client: TestClient, tmp_path: Path
) -> None:
    _seed_project(tmp_path)
    html = client.get("/project/alpha/node/double/panel").text
    # Both rows were authored `note` before `amount`; both tables show the schema's order,
    # so a carried-through column is under the same heading in each.
    input_table = html.split("<h3>input")[1].split("<h3>")[0]
    assert input_table.index("<th>amount</th>") < input_table.index("<th>note</th>")
    expected_table = html.split("<h3>expected output</h3>")[1].split("</table>")[0]
    assert expected_table.index("<th>amount</th>") < expected_table.index("<th>note</th>")
    assert expected_table.index("<th>note</th>") < expected_table.index(
        '<th class="test-col-written">doubled</th>'
    )


def test_panel_without_tests_has_no_tests_section(client: TestClient, tmp_path: Path) -> None:
    _seed_project(tmp_path)
    response = client.get("/project/alpha/node/load/panel")
    assert response.status_code == 200
    assert "test-report" not in response.text
