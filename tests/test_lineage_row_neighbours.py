"""A lone reached row is drawn among its neighbours, so it can be read against them."""
from __future__ import annotations


import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.services.project import save_working_copy_as_version
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
    save_working_copy_as_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def test_a_sheet_of_one_reached_row_draws_the_rows_around_it(run_id):
    # 55 - 25 // 2 = 43, and the sheet runs out of frame before its 25th row.
    page = _panel(run_id)
    assert "showing 17 of 60 rows" in page
    assert f'<td class="row-num muted">{43 + 1}</td>' in page
    assert f'<td class="row-num muted">{ROWS}</td>' in page


def test_the_panel_links_every_drawn_row_to_itself(run_id):
    page = _panel(run_id)
    # Row 43 is drawn first; a link built off the loop would send the reader to row 0.
    assert "/stage/double/row/43/trace/view" in page
    assert "/stage/double/row/0/trace/view" not in page
    assert f"/stage/double/row/{CITED_ROW}/trace/view" in page
    # One stage's panel is one request now, so the traced row is numbered once.
    assert page.count(f'<td class="row-num muted">{CITED_ROW + 1}</td>') == 1


def test_no_tint_reaches_this_pane_through_the_shared_diff(run_id):
    # `plain` is passed to _stage_diff.html here; the run page's diff keeps its own.
    page = _panel(run_id)
    for tint in ("diff-row-mine", "diff-row-num-mine", "diff-col-cited",
                 "diff-cell-changed", "diff-col-new", "diff-col-quiet"):
        assert tint not in page


def _panel(run_id):
    """The run page's own panel for `double`, cut to the rows behind the figure."""
    return TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/double/traced"
        f"?stage=double&row={CITED_ROW}&column=doubled").text
