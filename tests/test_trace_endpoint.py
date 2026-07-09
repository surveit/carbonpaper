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


def test_trace_view_renders_story_and_graph(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "Story" in body and "Graph" in body        # the two-view toggle
    assert "mermaid" in body                            # reuses the central graph
    assert "enrich" in body and "seeds" in body        # both stages, in the payload
    assert "score" in body                             # a new-at-stage column
    assert '"step": 1' in body and '"step": 2' in body  # numbered steps in payload


def test_trace_view_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace/view")
    assert resp.status_code == 404


def test_trace_view_says_not_supported_for_reshaping_stage(tmp_path, monkeypatch):
    """Starting a trace at a row-reshaping stage returns 200 with an explicit
    'not supported yet' banner — never a 500, never a wrong lineage."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"]})
    deduped = pd.DataFrame({"facility_id": ["a"]})  # a frame function reshaped rows
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "dedup", "type": "python_frame_function", "parents": ["seeds"], "df": deduped},
    ], run_id="R2")
    monkeypatch.setattr(loading, "EXAMPLES_DIR", tmp_path)
    resp = TestClient(app).get("/project/proj/runs/R2/stage/dedup/row/0/trace/view")
    assert resp.status_code == 200
    assert "not supported yet" in resp.text
    assert "#58" in resp.text
