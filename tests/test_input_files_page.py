"""The Input files tab's three routes, over tests/scope_fixture.py."""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient

from app.core.files import receive_source, resolve_stored_path
from app.main import app
from app.services import run as run_service
from scope_fixture import stage_specs, write_inputs
from stage_seed import save_version, set_stages

PROJECT = "input_files_page"
FETCHED = "input_files_fetched"
EAST_ORIGIN = "https://example.org/grants/east.csv"


@pytest.fixture
def run_id(projects_root):
    data = projects_root / PROJECT / "data"
    write_inputs(data)
    set_stages(PROJECT, stage_specs(data))
    save_version(PROJECT, message="fixture")
    return str(run_service.execute(PROJECT)["run_id"])


def _run_beside_a_fetched_east(projects_root, read_the_fetched_copy: bool):
    """The store holds a fetched copy of east; west is a loose file it never held."""
    data = projects_root / FETCHED / "data"
    write_inputs(data)
    with (data / "east.csv").open("rb") as stream:
        record = receive_source(FETCHED, EAST_ORIGIN, "east.csv", stream)
    specs = stage_specs(data)
    if read_the_fetched_copy:
        east = next(spec for spec in specs if spec["id"] == "load_east")
        east["connector"]["params"]["paths"] = [str(resolve_stored_path(record))]
    set_stages(FETCHED, specs)
    save_version(FETCHED, message="fixture")
    return record, str(run_service.execute(FETCHED)["run_id"])


def _url(run_id: str, leaf: str, project: str = PROJECT, **extra) -> str:
    query = {"stage": "grant_totals", "row": 0, "column": "total_amount", **extra}
    pairs = "&".join(f"{key}={value}" for key, value in query.items())
    return f"/project/{project}/runs/{run_id}/input-files/{leaf}?{pairs}"


def test_the_panel_names_each_file_and_both_toggles(run_id):
    page = TestClient(app).get(_url(run_id, "panel"))
    assert page.status_code == 200
    assert "east.csv" in page.text and "west.csv" in page.text
    assert "Relevant rows" in page.text and "All columns" in page.text


def test_the_panel_carries_a_shape_row_for_each_basis(run_id):
    page = TestClient(app).get(_url(run_id, "panel")).text
    assert 'data-basis="relevant"' in page and 'data-basis="all"' in page


def test_the_download_carries_the_relevant_rows_and_columns(run_id):
    answer = TestClient(app).get(_url(run_id, "slice.csv", file=0))
    assert answer.status_code == 200
    rows = list(csv.reader(io.StringIO(answer.text)))
    assert rows[0] == ["agency_code", "amount", "grant_id", "kind"]
    assert 1 < len(rows) < 7


def test_the_download_widens_to_every_row_and_column(run_id):
    answer = TestClient(app).get(
        _url(run_id, "slice.csv", file=0, rows="all", columns="all"))
    rows = list(csv.reader(io.StringIO(answer.text)))
    assert len(rows) == 7
    assert "region" in rows[0]


def test_a_file_this_figure_never_read_is_refused(run_id):
    answer = TestClient(app).get(_url(run_id, "slice.csv", file=9))
    assert answer.status_code == 404




def test_a_fetched_file_links_its_page_and_says_where_and_when_it_was_fetched(projects_root):
    record, run_id = _run_beside_a_fetched_east(projects_root, read_the_fetched_copy=True)

    page = TestClient(app).get(_url(run_id, "panel", project=FETCHED)).text

    assert f'href="/project/{FETCHED}/files/{record.id}"' in page
    assert f'href="{EAST_ORIGIN}"' in page and f'datetime="{record.fetched_at}"' in page
    # West was read off a file the store never held, so it has no page to link.
    assert "matches the bytes this run read" in page


def test_a_fetch_the_run_only_matches_by_bytes_is_linked_but_names_no_origin(projects_root):
    record, run_id = _run_beside_a_fetched_east(projects_root, read_the_fetched_copy=False)

    page = TestClient(app).get(_url(run_id, "panel", project=FETCHED)).text

    assert f'href="/project/{FETCHED}/files/{record.id}"' in page
    assert EAST_ORIGIN not in page
