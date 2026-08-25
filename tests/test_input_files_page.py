"""The Input files tab's three routes, over tests/scope_fixture.py."""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services import run as run_service
from app.services.project import save_working_copy_as_version
from scope_fixture import stage_specs, write_inputs
from stage_seed import set_stages

PROJECT = "input_files_page"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_working_copy_as_version(PROJECT, message="fixture", reviewer="test")
    return str(run_service.execute(PROJECT)["run_id"])


def _url(run_id: str, leaf: str, **extra) -> str:
    query = {"stage": "grant_totals", "row": 0, "column": "total_amount", **extra}
    pairs = "&".join(f"{key}={value}" for key, value in query.items())
    return f"/project/{PROJECT}/runs/{run_id}/input-files/{leaf}?{pairs}"


def test_the_panel_names_each_file_and_both_toggles(run_id):
    page = TestClient(app).get(_url(run_id, "panel"))
    assert page.status_code == 200
    assert "east.csv" in page.text and "west.csv" in page.text
    assert "Relevant rows" in page.text and "All columns" in page.text


def test_the_panel_carries_a_shape_row_for_each_basis(run_id):
    page = TestClient(app).get(_url(run_id, "panel")).text
    assert 'data-basis="relevant"' in page and 'data-basis="all"' in page


def test_the_download_carries_the_relevant_rows_and_columns(run_id):
    answer = TestClient(app).get(_url(run_id, "slice.csv", input="load_east"))
    assert answer.status_code == 200
    rows = list(csv.reader(io.StringIO(answer.text)))
    assert rows[0] == ["agency_code", "amount", "grant_id", "kind"]
    assert 1 < len(rows) < 7


def test_the_download_widens_to_every_row_and_column(run_id):
    answer = TestClient(app).get(
        _url(run_id, "slice.csv", input="load_east", rows="all", columns="all"))
    rows = list(csv.reader(io.StringIO(answer.text)))
    assert len(rows) == 7
    assert "region" in rows[0]


def test_a_file_this_figure_never_read_is_refused(run_id):
    answer = TestClient(app).get(_url(run_id, "slice.csv", input="load_nothing"))
    assert answer.status_code == 404


