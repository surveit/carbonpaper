"""A scoped panel draws the figure's own rows; the frame around them is the other pick."""
from __future__ import annotations

import re


import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.run as run_service
from app.main import app
from app.web.run_stage_view import SCOPED_ROWS_SHOWN
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


def test_the_figures_own_row_is_all_the_relevant_pick_draws(run_id):
    page = _panel(run_id)
    assert "1 × 1 behind this figure" in page
    # One row of the sixty, and one column of the two: the rest is the other pick.
    assert re.findall(r'class="row-num muted">(\d+)<', page) == [str(CITED_ROW + 1)]
    assert page.count('<span class="diff-col-name">') == 1


def test_the_frame_pick_opens_on_a_window_holding_that_row(run_id):
    page = _panel(run_id, rows="frame")
    # 55 - 25 // 2 = 43, so the window runs 43..59 and stops at the frame's end.
    drawn = [int(n) for n in re.findall(r'class="row-num muted">(\d+)<', page)]
    assert drawn == list(range(43 + 1, ROWS + 1))
    # Same page size either way, so the table does not resize under the reader.
    assert len(drawn) <= SCOPED_ROWS_SHOWN


def test_the_panel_links_every_drawn_row_to_itself(run_id):
    page = _panel(run_id, rows="frame")
    # Row 43 is drawn first; a link built off the loop would send the reader to row 0.
    assert "/stage/double/row/43/trace/view" in page
    assert "/stage/double/row/0/trace/view" not in page
    assert f"/stage/double/row/{CITED_ROW}/trace/view" in page
    # One stage's panel is one request now, so the traced row is numbered once.
    assert page.count(f'<td class="row-num muted">{CITED_ROW + 1}</td>') == 1


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
