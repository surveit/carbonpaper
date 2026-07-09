"""The trace endpoint returns the serialized trace, and maps tracer errors to
HTTP status codes. Uses a temp EXAMPLES_DIR so it needs no committed run."""
from __future__ import annotations

import pandas as pd
from fastapi.testclient import TestClient

import app.web.loading as loading
from app.main import app
from tests.test_trace_helpers import write_run


def _project_run(tmp_path, monkeypatch):
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    enrich = seeds.assign(score=[1, 2])
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R1")
    # runs_dir() resolves against loading.EXAMPLES_DIR; point it at our temp tree
    # (same pattern as tests/test_run_rows.py).
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    return TestClient(app)


def test_trace_endpoint_returns_serialized_trace(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert [h["stage_id"] for h in body["hops"]] == ["enrich", "seeds"]
    assert body["terminal"]["kind"] == "origin"


def test_trace_endpoint_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace")
    assert resp.status_code == 404


def test_trace_endpoint_400_for_out_of_range_row(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/9/trace")
    assert resp.status_code == 400


def test_trace_view_renders_html_hop_cards(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "enrich" in body and "seeds" in body      # both hops present
    assert "score" in body                            # a new-at-stage column
    assert "originate" in body                        # terminal message text


def test_trace_view_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace/view")
    assert resp.status_code == 404
