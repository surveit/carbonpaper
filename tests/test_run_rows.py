"""Tests for the full-table view of a stage's output rows and its CSV download.

Routes under test:
    GET /project/{m}/runs/{run_id}/stage/{stage_id}/rows      (HTML, capped)
    GET /project/{m}/runs/{run_id}/stage/{stage_id}/rows.csv  (full file)
"""
from __future__ import annotations

import io
import re
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.web.loading import render_frame_as_text
from app.main import app
from app.services import workspace
from run_seed import read_manifest, store_manifest

PROJ = "testmeth"
RUN = "run-0001"
STAGE = "stage_a"


def _write_run(
    examples_dir: Path, df: pd.DataFrame, fmt: str = "parquet"
) -> Path:
    run_dir = examples_dir / PROJ / "runs" / RUN
    (run_dir / "outputs").mkdir(parents=True)
    output_rel = f"outputs/{STAGE}.{fmt}"
    if fmt == "parquet":
        df.to_parquet(run_dir / output_rel, index=False)
    else:
        df.to_csv(run_dir / output_rel, index=False)
    manifest = {
        "run_id": RUN,
        "started_at": RUN,
        "project": PROJ,
        "workflow_version": RUN,
        "status": "ok",
        "human_review_queue_stats": {},
        "stage_records": [
            {
                "stage_id": STAGE,
                "type": "input_data",
                "description": STAGE,
                "status": "ok",
                "input_validation_report": [],
                "output_validation_report": None,
                "output_row_count": len(df),
                "output_path": output_rel,
            }
        ],
    }
    store_manifest(run_dir.parent.parent, run_dir.name, manifest)
    return run_dir


@pytest.fixture()
def examples_dir(tmp_path: Path, monkeypatch) -> Path:
    workspace.set_projects_dir(tmp_path)
    return tmp_path


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


def _df(n: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "name": [f"rowval_{i:04d}" for i in range(n)],
            "url": [f"https://example.com/{i}" for i in range(n)],
        }
    )


def test_rows_page_shows_all_rows_under_cap(examples_dir, client):
    _write_run(examples_dir, _df(3))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 200
    for i in range(3):
        assert f"rowval_{i:04d}" in r.text
    # no truncation notice when everything fits
    assert "Showing first" not in r.text


def test_rows_page_links_each_row_to_its_trace(examples_dir, client):
    _write_run(examples_dir, _df(3))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 200
    # every rendered row has a 0-indexed "View lineage" trace link
    assert "View lineage" in r.text
    for i in range(3):
        assert f"/stage/{STAGE}/row/{i}/trace/view" in r.text


def test_the_number_shown_counts_from_one_and_the_link_from_zero(examples_dir, client):
    _write_run(examples_dir, _df(3))
    body = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows").text
    numbers = re.findall(r'<td class="row-num muted">(\d+)</td>', body)
    # The reader counts rows as a spreadsheet does; the address carries the ordinal.
    assert numbers == ["1", "2", "3"]
    assert f"/stage/{STAGE}/row/0/trace/view" in body
    assert f"/stage/{STAGE}/row/3/trace/view" not in body


def test_rows_page_filters_to_named_ordinals(examples_dir, client):
    _write_run(examples_dir, _df(5))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?ordinals=1,3")
    assert r.status_code == 200
    assert "rowval_0001" in r.text and "rowval_0003" in r.text
    assert "rowval_0000" not in r.text and "rowval_0002" not in r.text
    assert "Showing 2 of 5 rows" in r.text


def test_a_filtered_row_keeps_its_own_trace_link(examples_dir, client):
    _write_run(examples_dir, _df(5))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?ordinals=3")
    # The link opens row 3, not row 0 — a position in the filtered list would
    # trace a different row than the one the reader is looking at.
    assert f"/stage/{STAGE}/row/3/trace/view" in r.text
    assert f"/stage/{STAGE}/row/0/trace/view" not in r.text


def test_an_out_of_range_or_unparseable_ordinal_is_skipped(examples_dir, client):
    _write_run(examples_dir, _df(3))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?ordinals=1,99,abc")
    assert r.status_code == 200
    assert "rowval_0001" in r.text


def test_load_output_row_scopes_to_one_row(tmp_path):
    from app.web.loading import load_output_row
    (tmp_path / "outputs").mkdir()
    pd.DataFrame({"a": [10, 20, 30]}).to_parquet(tmp_path / "outputs/o.parquet", index=False)
    got = load_output_row(tmp_path, "outputs/o.parquet", 1)
    assert got["preview"] == [{"a": "20"}] and got["rows_total"] == 3
    past = load_output_row(tmp_path, "outputs/o.parquet", 9)
    assert past["out_of_range"] is True and past["preview"] == []
    assert load_output_row(tmp_path, None, 0) is None


def test_rows_page_caps_rendered_rows(examples_dir, client, monkeypatch):
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 10)
    _write_run(examples_dir, _df(25))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 200
    assert "rowval_0009" in r.text  # last row inside the cap
    assert "rowval_0010" not in r.text  # first row beyond the cap
    # truncation notice names both the cap and the true total
    assert "Showing first" in r.text
    assert "10" in r.text and "25" in r.text


def test_csv_download_is_full_file_ignoring_cap(examples_dir, client, monkeypatch):
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 10)
    _write_run(examples_dir, _df(25))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    assert f"{STAGE}.csv" in r.headers["content-disposition"]
    got = pd.read_csv(io.BytesIO(r.content))
    assert len(got) == 25
    assert list(got["name"]) == [f"rowval_{i:04d}" for i in range(25)]


def test_csv_download_from_csv_output(examples_dir, client):
    _write_run(examples_dir, _df(4), fmt="csv")
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    assert r.status_code == 200
    got = pd.read_csv(io.BytesIO(r.content))
    assert len(got) == 4


def _accented_df() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "média": ["Ce sac ne mérite qu'une chose", "Offrons un verre à Pascal"],
            "text": ["zelfmoord plegen, scheelt ook een hoop 🤝", "députés et sénateurs"],
        }
    )


def test_csv_download_opens_with_a_utf8_byte_order_mark(examples_dir, client):
    """Without it Excel on Windows falls back to its legacy code page and shows mérite as mÃ©rite."""
    _write_run(examples_dir, _accented_df())
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    assert r.status_code == 200
    assert r.content.startswith(b"\xef\xbb\xbf")
    # The bytes after the mark are plain UTF-8 — the mark is a prefix, not a
    # re-encoding, so the accented text is unchanged.
    assert "mérite" in r.content[3:].decode("utf-8")
    assert "🤝" in r.content[3:].decode("utf-8")


def test_csv_download_reimports_without_the_mark_in_a_column_name(examples_dir, client):
    _write_run(examples_dir, _accented_df())
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    got = pd.read_csv(io.BytesIO(r.content))
    assert list(got.columns) == ["média", "text"]
    assert got["média"][0] == "Ce sac ne mérite qu'une chose"


def test_rows_404_for_unknown_run(examples_dir, client):
    _write_run(examples_dir, _df(2))
    r = client.get(f"/project/{PROJ}/runs/no-such-run/stage/{STAGE}/rows")
    assert r.status_code == 404


def test_rows_404_for_unknown_stage(examples_dir, client):
    _write_run(examples_dir, _df(2))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/nope/rows")
    assert r.status_code == 404


def test_rows_404_when_output_file_missing(examples_dir, client):
    run_dir = _write_run(examples_dir, _df(2))
    (run_dir / "outputs" / f"{STAGE}.parquet").unlink()
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]


def test_rows_rejects_output_path_outside_run_dir(examples_dir, client):
    run_dir = _write_run(examples_dir, _df(2))
    manifest = read_manifest(run_dir.parent.parent, run_dir.name)
    manifest["stage_records"][0]["output_path"] = "../../../../etc/passwd"
    store_manifest(run_dir.parent.parent, run_dir.name, manifest)
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 404


def test_rows_page_renders_a_nullable_int_column(examples_dir, client):
    frame = pd.DataFrame({
        "name": ["rowval_0000", "rowval_0001"],
        "likes": pd.Series([None, 7], dtype="Int64"),
    })
    _write_run(examples_dir, frame)
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 200
    assert "rowval_0000" in r.text and "7" in r.text


@pytest.mark.parametrize("dtype,values,expected", [
    ("Int64", [1, None], ["1", ""]),
    ("Float64", [1.5, None], ["1.5", ""]),
    ("boolean", [True, None], ["True", ""]),
    ("object", ["x", None], ["x", ""]),
])
def test_a_masked_dtype_renders_its_null_as_blank(dtype, values, expected):
    frame = pd.DataFrame({"c": pd.Series(values, dtype=dtype)})
    assert list(render_frame_as_text(frame)["c"]) == expected


def test_a_sequence_cell_survives_beside_a_masked_null():
    frame = pd.DataFrame({
        "tags": [["a", "b"], []],
        "likes": pd.Series([None, None], dtype="Int64"),
    })
    text = render_frame_as_text(frame)
    assert list(text["tags"]) == ["['a', 'b']", "[]"]
    assert list(text["likes"]) == ["", ""]


def test_a_date_keeps_its_compact_rendering_and_a_missing_one_is_blank():
    frame = pd.DataFrame({"d": pd.to_datetime(["2026-01-01", None])})
    assert list(render_frame_as_text(frame)["d"]) == ["2026-01-01", ""]


# ─── The rectangle: a published table's address inside the output ────────────

def _rows(client, query: str = "") -> str:
    return client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows{query}").text


def test_a_rectangle_draws_the_rows_it_names_and_no_others(examples_dir, client):
    _write_run(examples_dir, _df(10))
    body = _rows(client, "?rows=2:5")
    assert "rowval_0002" in body and "rowval_0004" in body
    assert "rowval_0001" not in body and "rowval_0005" not in body


def test_a_rectangle_draws_the_columns_it_names_and_no_others(examples_dir, client):
    _write_run(examples_dir, _df(3))
    body = _rows(client, "?columns=name")
    assert "rowval_0000" in body
    assert "https://example.com/0" not in body


def test_a_rectangle_row_keeps_the_ordinal_it_has_in_the_frame(examples_dir, client):
    _write_run(examples_dir, _df(10))
    body = _rows(client, "?rows=4:6")
    assert f"/stage/{STAGE}/row/4/trace/view" in body
    assert f"/stage/{STAGE}/row/0/trace/view" not in body


def test_a_rectangle_offers_the_whole_output_beside_it(examples_dir, client):
    _write_run(examples_dir, _df(10))
    assert "Show the whole output instead" in _rows(client, "?rows=0:2")
    assert "Show the whole output instead" not in _rows(client)


def test_naming_rows_two_ways_at_once_is_refused(examples_dir, client):
    _write_run(examples_dir, _df(10))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?ordinals=1,2&rows=0:2")
    assert r.status_code == 400


def test_a_row_range_that_is_not_start_end_is_refused(examples_dir, client):
    _write_run(examples_dir, _df(10))
    assert client.get(
        f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?rows=first-two").status_code == 400


def test_a_column_the_output_does_not_hold_is_refused(examples_dir, client):
    _write_run(examples_dir, _df(3))
    assert client.get(
        f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows?columns=retainer").status_code == 404


def test_a_row_range_past_the_end_stops_at_the_end(examples_dir, client):
    _write_run(examples_dir, _df(3))
    body = _rows(client, "?rows=1:99")
    assert "rowval_0002" in body and "rowval_0000" not in body


def test_the_csv_serves_the_rectangle_it_was_asked_for(examples_dir, client):
    _write_run(examples_dir, _df(10))
    r = client.get(
        f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv?rows=2:5&columns=name")
    frame = pd.read_csv(io.StringIO(r.text))
    assert list(frame.columns) == ["name"]
    assert list(frame["name"]) == ["rowval_0002", "rowval_0003", "rowval_0004"]


def test_the_csv_still_serves_the_whole_output_when_asked_for_nothing(examples_dir, client):
    _write_run(examples_dir, _df(10))
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    frame = pd.read_csv(io.StringIO(r.text))
    assert list(frame.columns) == ["name", "url"] and len(frame) == 10
