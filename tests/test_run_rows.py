"""Tests for the full-table view of a stage's output rows and its CSV download.

Routes under test:
    GET /project/{m}/runs/{run_id}/stage/{stage_id}/rows      (HTML, capped)
    GET /project/{m}/runs/{run_id}/stage/{stage_id}/rows.csv  (full file)
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.main import app

PROJ = "testmeth"
RUN = "run-0001"
STAGE = "stage_a"


def _write_run(
    examples_dir: Path, df: pd.DataFrame, fmt: str = "parquet"
) -> Path:
    """Lay out examples/<PROJ>/runs/<RUN>/ with a manifest + one stage output."""
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
        "stages": [
            {
                "stage_id": STAGE,
                "type": "input_data",
                "name": STAGE,
                "status": "ok",
                "rows": len(df),
                "output_path": output_rel,
            }
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run_dir


@pytest.fixture()
def examples_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
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
    got = pd.read_csv(io.StringIO(r.text))
    assert len(got) == 25
    assert list(got["name"]) == [f"rowval_{i:04d}" for i in range(25)]


def test_csv_download_from_csv_output(examples_dir, client):
    _write_run(examples_dir, _df(4), fmt="csv")
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows.csv")
    assert r.status_code == 200
    got = pd.read_csv(io.StringIO(r.text))
    assert len(got) == 4


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
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest["stages"][0]["output_path"] = "../../../../etc/passwd"
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    r = client.get(f"/project/{PROJ}/runs/{RUN}/stage/{STAGE}/rows")
    assert r.status_code == 404
