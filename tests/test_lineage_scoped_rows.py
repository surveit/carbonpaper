"""A scoped panel draws the figure's own rows; the frame around them is the other pick."""
from __future__ import annotations

import re


import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.web.run_stage_view import SCOPED_ROWS_SHOWN
from scope_fixture import column
from stage_seed import save_version, set_stages

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
    save_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def test_the_figures_own_row_is_all_the_relevant_pick_draws(run_id):
    page = _panel(run_id)
    # `doubled` is the figure's, and `amount` is what this stage read to write it.
    assert "1 × 2 behind this figure" in page
    assert _row_numbers(page) == [CITED_ROW + 1]


def test_the_frame_pick_draws_the_frame_around_that_row(run_id):
    page = _panel(run_id, rows="frame")
    assert _row_numbers(page) == list(range(1, ROWS + 1))
    # Same page either way, so the table does not resize under the reader.
    assert len(_row_numbers(page)) <= SCOPED_ROWS_SHOWN


def test_the_panel_links_its_one_drawn_row_to_itself(run_id):
    page = _panel(run_id)
    # A link built off the loop would send the reader to row 0 for the only row drawn.
    assert f"/stage/double/row/{CITED_ROW}/trace/view" in page
    assert "/stage/double/row/0/trace/view" not in page
    assert page.count(f'<td class="row-num muted">{CITED_ROW + 1}</td>') == 1


def _row_numbers(page):
    return [int(n) for n in re.findall(r'class="row-num muted">(\d+)<', page)]


def test_the_pane_keeps_the_run_page_tints_and_adds_its_own(run_id):
    # The shared diff paints here as it does on the run page; the figure's marks come on top.
    page = _panel(run_id, rows="frame")
    for tint in ("diff-col-new", "diff-col-cited"):
        assert tint in page
    # The cited cell: row 55's doubled amount, in the column the stage added.
    assert f'<td class="diff-col-new diff-col-cited">{CITED_ROW * 20}</td>' in page
    assert page.count("diff-col-cited") == 1


def _panel(run_id, rows="figure"):
    """The run page's own panel for `double`, cut to the rows behind the figure."""
    return TestClient(app).get(
        f"/project/{PROJECT}/runs/{run_id}/stage/double/traced"
        f"?stage=double&row={CITED_ROW}&column=doubled&rows={rows}").text
