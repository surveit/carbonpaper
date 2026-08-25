"""A lone reached row is drawn among its neighbours, so it can be read against them."""
from __future__ import annotations

import re

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
from app.web.values_view import load_values_used
from scope_fixture import column
from stage_seed import set_stages

PROJECT = "row_neighbours_fixture"
ROWS = 60
CITED_ROW = 55


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    data.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"amount": [i * 10 for i in range(ROWS)]}).to_csv(
        data / "grants.csv", index=False)
    set_stages(PROJECT, [
        {"id": "load_grants", "type": "input_data", "cache": True,
         "description": "Sixty numbered grants, one per row.",
         "connector": {"kind": "file", "params": {
             "paths": [str(data / "grants.csv")], "format": "csv"}},
         "signature": {"form": "replaces", "produces": [column("amount", "int", False)]}},
        {"id": "double", "type": "python_row_function", "cache": True,
         "description": "Doubles the amount recorded on each grant.",
         "inputs": [{"id": "load_grants"}],
         "function": {"kind": "inline",
                      "code": "def transform(row):\n    return {'doubled': row['amount'] * 2}\n"},
         "signature": {"form": "extends",
                       "reads": [{"input": "load_grants",
                                  "columns": [column("amount", "int", False)]}],
                       "adds": [column("doubled", "int", False)], "rewrites": []}},
    ])
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def _steps(run_id):
    values = load_values_used(PROJECT, run_id, "double", "doubled", row=CITED_ROW)
    return {step.stage_id: step for step in values.steps}


def test_a_sheet_of_one_reached_row_draws_the_rows_around_it(run_id):
    # 55 - 25 // 2 = 43, and the sheet runs out of frame before its 25th row.
    for step in _steps(run_id).values():
        assert step.reached_rows == [CITED_ROW]
        assert step.row_ordinals == list(range(43, 60))


def test_the_panel_links_every_drawn_row_to_itself_and_marks_the_one_traced(run_id):
    page = _panel(run_id)
    assert "showing 17 of 60 rows" in page
    # Row 43 is drawn first; a link built off the loop would send the reader to row 0.
    assert "/stage/double/row/43/trace/view" in page
    assert "/stage/double/row/0/trace/view" not in page
    assert f"/stage/double/row/{CITED_ROW}/trace/view" in page
    # One mark per sheet: the input stage's, and the stage the figure was taken from.
    assert page.count("diff-row-mine") == 2
    assert page.count(f'<td class="row-num diff-row-num-mine">{CITED_ROW + 1}</td>') == 2


def test_the_cited_column_is_painted_on_the_traced_row_alone(run_id):
    cells = re.findall(r'<td[^>]*class="[^"]*diff-col-cited', _panel(run_id))
    assert len(cells) == 1


def _panel(run_id):
    return TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/values/panel"
        f"?stage=double&row={CITED_ROW}&column=doubled").text
