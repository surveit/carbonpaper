"""The trace endpoint returns the serialized trace, and maps tracer errors to
HTTP status codes. Uses a temp projects root so it needs no committed run."""
from __future__ import annotations

import json
import math

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
from test_trace_helpers import write_run
from app.services import workspace


def _project_run(tmp_path, monkeypatch):
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    enrich = seeds.assign(score=[1, 2])
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R1")
    # runs_dir() resolves against the projects root; point it at our temp tree
    # (same pattern as tests/test_run_rows.py).
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_trace_endpoint_returns_serialized_trace(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace")
    assert resp.status_code == 200
    body = resp.json()
    assert [s["stage_id"] for s in body["steps"]] == ["enrich", "seeds"]
    assert body["end"]["reached_origin"] is True


def test_trace_endpoint_encodes_nan_and_infinity_as_null(tmp_path, monkeypatch):
    # A nullable numeric column (income/expenses on a lobbying-disclosure
    # dataset, legitimately absent on most rows) arrives as pandas NaN; a 500
    # here means any dataset with a nullable numeric column is unreachable.
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})
    enrich = seeds.assign(income=[math.nan, math.inf, -math.inf])
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ], run_id="R3")
    workspace.set_projects_dir(tmp_path)
    client = TestClient(app)
    for row, expected in enumerate([None, None, None]):
        resp = client.get(f"/project/proj/runs/R3/stage/enrich/row/{row}/trace")
        assert resp.status_code == 200
        # Strict JSON: json.loads must not choke, and the raw body must carry
        # the standard `null` token, never the non-standard `NaN`/`Infinity`.
        assert json.loads(resp.text)["steps"][0]["row"]["income"] is expected
        assert "NaN" not in resp.text and "Infinity" not in resp.text


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
    assert "stage-panel" in body                        # loads the shared stage panel
    assert "/lineage_panel?row=" in body                # trimmed to the traced row
    assert 'data-disc="transform"' in body              # transform-link target
    assert 'data-disc="output"' in body                 # row-chip target
    assert "filtered to this single row" in body        # single-row subheader
    assert "enrich" in body and "seeds" in body        # both stages, in the payload
    assert '"step": 1' in body and '"step": 2' in body  # numbered steps in payload


def test_trace_view_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace/view")
    assert resp.status_code == 404


def test_lineage_panel_is_trimmed_to_the_row(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # enrich rows: a/A, b/B (+score)
    resp = client.get("/project/proj/runs/R1/stage/enrich/lineage_panel?row=1")
    assert resp.status_code == 200
    body = resp.text
    assert "row 1" in body                    # scoped badge
    assert ">b<" in body or "b</td>" in body  # row 1's data (facility_id b)
    assert "A" not in body.split("Output")[-1]  # row 0's data not in the output table


def test_trace_view_says_reshaping_not_traceable(tmp_path, monkeypatch):
    """Starting a trace at a row-reshaping stage returns 200 with the reshaping
    stop message (points at #58) — never a 500, never a wrong lineage."""
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    seeds = pd.DataFrame({"facility_id": ["a", "b"]})
    deduped = pd.DataFrame({"facility_id": ["a"]})  # a frame function reshaped rows
    write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "dedup", "type": "python_frame_function", "parents": ["seeds"], "df": deduped},
    ], run_id="R2")
    workspace.set_projects_dir(tmp_path)
    resp = TestClient(app).get("/project/proj/runs/R2/stage/dedup/row/0/trace/view")
    assert resp.status_code == 200
    assert "reshapes rows" in resp.text
    assert "#58" in resp.text
