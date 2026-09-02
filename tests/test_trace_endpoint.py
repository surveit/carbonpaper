"""The trace endpoint returns the serialized trace, and maps tracer errors to
HTTP status codes. Uses a temp projects root so it needs no committed run."""
from __future__ import annotations

import json
import math

import pandas as pd
from fastapi.testclient import TestClient

from app.main import app
from app.runtime.runner import execute_run
from app.services import project as project_service
from app.services import workspace
from conftest import pinned_stages
from stage_seed import add_stage
from test_trace_helpers import write_run


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
    # A nullable numeric column arrives as pandas NaN; a 500 here makes any
    # dataset with one unreachable.
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


def test_trace_view_renders_the_story_and_the_row_panel(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "mermaid" in body                            # reuses the central graph
    assert "/lineage_panel?row=" in body                # the Transform tab's fetch
    assert '"sampled": null' in body
    assert "enrich" in body and "seeds" in body        # both stages, in the payload
    assert '"step": 1' in body and '"step": 2' in body  # numbered steps in payload
    assert '"row_diff"' in body                         # the row, marked, in the payload
    assert '/runs/R1#enrich' in body                    # back to the stage in the run


def test_the_graph_is_folded_away_and_the_row_is_not(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    body = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view").text
    graph = body.split('class="lin-graph"')[1][:40]
    assert "open" not in graph


def test_trace_view_404_for_unknown_stage(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get("/project/proj/runs/R1/stage/nope/row/0/trace/view")
    assert resp.status_code == 404


def test_lineage_panel_is_the_transform_not_the_row(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # enrich rows: a/A, b/B (+score)
    resp = client.get("/project/proj/runs/R1/stage/enrich/lineage_panel?row=1")
    assert resp.status_code == 200
    body = resp.text
    assert "lineage-stage" in body     # the panel rendered
    assert "data-preview" not in body  # and carries no row table
    assert "b</td>" not in body        # not row 1's cells either


def test_trace_view_headline_names_the_cited_cell(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # enrich row 1: b/B, score 2
    body = client.get(
        "/project/proj/runs/R1/stage/enrich/row/1/trace/view?column=score").text
    # The header's figure and column are painted from this option.
    assert '<option value="score" data-name="2" data-meta="score" selected>' in body
    # This run pins a version nothing can load, so the mark beside the column says so.
    assert "is unreadable, so nothing declares score here" in body


def test_the_header_offers_every_stage_this_run_wrote_rows_for(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # seeds and enrich, 2 rows each
    body = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace/view").text
    assert '<option value="seeds" data-side="2 rows"' in body
    assert '<option value="enrich" data-side="2 rows" data-meta="dangerously run code" selected>' in body


def test_the_header_bounds_the_row_box_by_the_stage_it_reads(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # enrich holds two rows
    body = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace/view").text
    # Ordinal 1 in the address is row 2 in the box, and 2 is the last one to ask for.
    assert 'value="2" min="1"' in body and 'max="2"' in body
    assert "of 2" in body


def test_the_header_offers_the_columns_of_the_row_it_read(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    body = client.get("/project/proj/runs/R1/stage/enrich/row/1/trace/view").text
    # Every option carries the cell it holds, because the trigger is painted from it.
    for column, value in [("facility_id", "b"), ("name", "B"), ("score", "2")]:
        assert f'<option value="{column}" data-name="{value}" data-meta="{column}"' in body


def test_a_row_the_walk_could_not_read_names_no_column(tmp_path):
    client = _run_missing_its_output_frame(tmp_path)
    body = client.get("/project/proj/runs/R4/stage/seeds/row/0/trace/view").text
    # The manifest recorded rows for the stage, so it is still somewhere to go.
    assert '<option value="seeds" data-side="2 rows" data-meta="input" selected>' in body
    assert 'id="lin-column"' not in body  # but no row was read, so no column to name


def test_trace_view_400_for_a_column_the_stage_does_not_have(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    resp = client.get(
        "/project/proj/runs/R1/stage/enrich/row/1/trace/view?column=nope")
    assert resp.status_code == 400
    assert "nope" in resp.json()["detail"] and "enrich" in resp.json()["detail"]


def test_trace_view_without_a_column_offers_the_whole_row(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)  # enrich: facility_id, name, score
    body = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view").text
    assert '<option value="" data-name="the whole row" data-meta="3 columns" selected>' in body
    assert "<strong>Select from 3 columns</strong>" in body
    # No cell is being read, so nothing is declared and the mark stays down.
    assert ' hidden>?</span>' in body


def test_trace_view_carries_the_three_tabs_with_the_story_open(tmp_path, monkeypatch):
    client = _project_run(tmp_path, monkeypatch)
    body = client.get("/project/proj/runs/R1/stage/enrich/row/0/trace/view").text
    assert '<button class="lin-pagetab on" data-pane="paths">Paths' in body
    for pane, label in [("rows", "Relevant rows"), ("values", "Relevant columns"), ("inputs", "Input files")]:
        assert f'data-pane="{pane}">{label}' in body
    # Relevant rows holds the scope map, in a frame the tab loads when it is opened.
    assert 'id="scope-frame"' in body
    # Relevant columns holds the column walk, fetched here on the same signal.
    assert 'id="values-root"' in body


VAL_DESCRIPTION = "Tonnes of CO2e the operator reported for the year."


def _run_a_pinned_version(tmp_path) -> tuple[TestClient, str]:
    project_dir = tmp_path / "described"
    project_dir.mkdir(parents=True)
    data = project_dir / "rows.csv"
    pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}).to_csv(data, index=False)
    add_stage(project_dir, {
        "id": "readings", "description": "Readings", "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(data), "format": "csv"}},
        "signature": {"form": "replaces", "produces": [
            {"name": "name", "type": "str", "nullable": False},
            {"name": "val", "type": "int", "nullable": False,
             "description": VAL_DESCRIPTION},
        ]},
    })
    workspace.set_projects_dir(tmp_path)
    project_service.save_working_copy_as_version(
        "described", message="v1").version_id
    run = execute_run(project_dir / "runs", "described", *pinned_stages(project_dir))
    return TestClient(app), str(run["run_id"])


def test_the_header_carries_the_declared_description_of_the_column_it_shows(tmp_path):
    client, run_id = _run_a_pinned_version(tmp_path)
    body = client.get(
        f"/project/described/runs/{run_id}/stage/readings/row/0/trace/view?column=val").text
    assert f'data-tip="{VAL_DESCRIPTION}"' in body
    assert '<option value="val" data-name="1" data-meta="val" selected>' in body


def test_an_undescribed_column_says_so_rather_than_showing_nothing(tmp_path):
    client, run_id = _run_a_pinned_version(tmp_path)
    body = client.get(
        f"/project/described/runs/{run_id}/stage/readings/row/0/trace/view?column=name").text
    assert "Declared str, not null. No description was authored for this column." in body


def test_trace_view_says_reshaping_not_traceable(tmp_path, monkeypatch):
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


def _run_missing_its_output_frame(tmp_path) -> TestClient:
    project_runs = tmp_path / "proj" / "runs"
    project_runs.mkdir(parents=True)
    run_dir = write_run(project_runs, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"facility_id": ["a", "b"]})},
    ], run_id="R4")
    # What an interrupted run leaves: a record naming a file nothing wrote.
    (run_dir / "outputs" / "seeds.parquet").unlink()
    workspace.set_projects_dir(tmp_path)
    return TestClient(app)


def test_trace_view_states_the_missing_output_file_instead_of_raising(tmp_path):
    client = _run_missing_its_output_frame(tmp_path)
    resp = client.get("/project/proj/runs/R4/stage/seeds/row/0/trace/view")
    assert resp.status_code == 200
    assert "this stage&#39;s output file is missing from the run" in resp.text
    assert "This run recorded no path for this row" in resp.text
    # The step machinery reads V.nodes[0]; with none, it must not ship at all.
    assert "renderStories();" not in resp.text


def test_trace_view_of_a_missing_output_file_still_renders_with_a_column(tmp_path):
    client = _run_missing_its_output_frame(tmp_path)
    resp = client.get(
        "/project/proj/runs/R4/stage/seeds/row/0/trace/view?column=facility_id")
    assert resp.status_code == 200
    assert "this stage&#39;s output file is missing from the run" in resp.text


def test_trace_of_a_missing_output_file_walks_no_step(tmp_path):
    client = _run_missing_its_output_frame(tmp_path)
    body = client.get("/project/proj/runs/R4/stage/seeds/row/0/trace").json()
    assert body["steps"] == []
    assert body["end"] == {
        "reached_origin": False,
        "at_stage": "seeds",
        "message": "this stage's output file is missing from the run",
    }
