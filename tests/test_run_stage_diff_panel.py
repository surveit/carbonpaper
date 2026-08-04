"""The run stage panel's Data pane through the real runner: a 1:1 stage reads
as a diff against its input (added columns tinted, changed cells marked), a
filter stage reads as one merged table with its dropped rows in place, the L2
strip is Data | Transform for every stage type, and an unverifiable alignment
falls back to the plain output view rather than guessing."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
from app.main import app
from app.runtime.lineage import lineage_sidecar_path
from app.runtime.runner import execute_run
from app.services import versioning
from app.services import project as project_service
from conftest import pinned_stages

client = TestClient(app)

PROJECT = "stage_diff_panel"
LOAD_ID = "load"
CLASSIFY_ID = "classify"
KEEP_ID = "keep"

_LOAD_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                            {"name": "val", "type": "int", "nullable": True}]}
_CLASSIFY_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                                {"name": "val", "type": "int", "nullable": True},
                                {"name": "label", "type": "str", "nullable": True}]}

# Uppercases `name` where val > 1 (a changed cell) and adds `label` (an added
# column), so the classify diff has one of each to show.
_CLASSIFY_CODE = (
    "def transform(row):\n"
    "    name = row['name'].upper() if row['val'] > 1 else row['name']\n"
    "    return {**row, 'name': name, 'label': 'big' if row['val'] > 1 else 'small'}\n"
)

_KEEP_CODE = (
    "def should_include(row):\n"
    "    return row['val'] != 2\n"
)


def _seed_compiled(pdir: Path, data_path: Path) -> None:
    compiled = pdir / "compiled"
    compiled.mkdir(parents=True)
    stages = [
        ("01_load.json", {
            "id": LOAD_ID, "name": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(data_path), "format": "csv"}},
            "output_schema": _LOAD_SCHEMA,
        }),
        ("02_classify.json", {
            "id": CLASSIFY_ID, "name": "Classify", "type": "python_row_function",
            "inputs": [{"id": LOAD_ID, "schema": _LOAD_SCHEMA}],
            "function": {"kind": "inline", "code": _CLASSIFY_CODE},
            "output_schema": _CLASSIFY_SCHEMA,
        }),
        ("03_keep.json", {
            "id": KEEP_ID, "name": "Keep the small ones", "type": "filter_rows",
            "inputs": [{"id": CLASSIFY_ID, "schema": _CLASSIFY_SCHEMA}],
            "filter": {"code": _KEEP_CODE},
            "output_schema": _CLASSIFY_SCHEMA,
        }),
    ]
    for filename, spec in stages:
        (compiled / filename).write_text(json.dumps(spec), encoding="utf-8")


@pytest.fixture()
def run_ctx(tmp_path: Path) -> tuple[Path, str]:
    pdir = tmp_path / PROJECT
    pdir.mkdir()
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["alpha", "beta", "gamma"], "val": [1, 2, 1]}).to_csv(
        data, index=False)
    _seed_compiled(pdir, data)
    workspace.set_projects_dir(tmp_path)
    version_id = project_service.save_working_copy_as_version(
        pdir, message="v1", reviewer="test").version_id
    versioning.publish_version(pdir, version_id, reviewer="test")
    run_id = str(execute_run(pdir, pdir, *pinned_stages(pdir))["run_id"])
    return pdir, run_id


def _panel(run_id: str, stage_id: str) -> str:
    response = client.get(f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/partial")
    assert response.status_code == 200
    return response.text


def test_a_row_function_output_reads_as_a_diff_against_its_input(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, CLASSIFY_ID)
    assert "output as a diff against its input" in html
    # The added column is tinted AND named in words (caption + summary).
    assert "diff-col-new" in html
    assert "added by this stage, carried by no input" in html
    assert "<code>label</code>" in html
    # The one uppercased name is a marked changed cell carrying its old value.
    assert "diff-cell-changed" in html
    assert "BETA" in html and "beta" in html
    # The diff table stands in for the plain output preview.
    assert "output data" not in html


def test_the_diff_header_links_both_raw_frames(run_ctx) -> None:
    """With the Inputs/Outputs tabs gone, both raw frames stay one click away."""
    # The header names input → stage and links each side's existing full-rows
    # view and CSV download — no new endpoints.
    _pdir, run_id = run_ctx
    html = _panel(run_id, CLASSIFY_ID)
    assert "raw input:" in html and "raw output:" in html
    assert f"/stage/{LOAD_ID}/rows.csv" in html
    assert f"/stage/{CLASSIFY_ID}/rows.csv" in html


def test_a_filter_stage_shows_its_dropped_rows_in_place(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, KEEP_ID)
    # ONE merged table: kept rows with lineage links, the dropped row inline,
    # tinted, carrying its input ordinal — no second table to reconcile.
    assert "dropped rows shown in place" in html
    assert "diff-row-dropped" in html
    assert "dropped · input row 1" in html
    assert "BETA" in html  # the dropped row's content is visible
    assert "View lineage" in html  # kept rows keep their lineage links
    assert "output data" not in html  # the merged table IS the output view


def test_the_two_tab_strip_replaces_inputs_and_outputs(run_ctx) -> None:
    _pdir, run_id = run_ctx
    for stage_id in (CLASSIFY_ID, LOAD_ID):  # in-scope and out-of-scope alike
        html = _panel(run_id, stage_id)
        assert 'data-l2="data"' in html and 'data-l2="transform"' in html
        assert 'data-l2="inputs"' not in html and 'data-l2="outputs"' not in html
        assert 'data-pane="schema-data"' in html and 'data-pane="run-data"' in html
        assert "run-inputs" not in html and "run-outputs" not in html
        assert "schema-inputs" not in html and "schema-outputs" not in html


def test_the_data_pane_keeps_the_input_row_picker(run_ctx) -> None:
    """The scratch row picker that lived under Inputs moved into Data, not away."""
    # It still drives the Transform pane's scratch preview.
    _pdir, run_id = run_ctx
    html = _panel(run_id, CLASSIFY_ID)
    assert "data-inputs" in html
    assert 'class="row-pick"' in html and "scratch-run" in html
    assert "Run transform on selected" in html


def test_an_input_stage_keeps_the_plain_output_view_in_data(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, LOAD_ID)
    assert "stage-diff" not in html
    assert "output data" in html
    # Its schema pane carries the output schema the old Outputs tab showed.
    assert "output schema" in html


def test_a_filter_missing_its_sidecar_falls_back_to_the_plain_pane(run_ctx) -> None:
    pdir, run_id = run_ctx
    lineage_sidecar_path(pdir / "runs" / run_id, KEEP_ID).unlink()
    html = _panel(run_id, KEEP_ID)
    assert "dropped rows shown in place" not in html
    assert "output data" in html


def test_a_row_count_mismatch_falls_back_to_the_plain_pane(run_ctx) -> None:
    pdir, run_id = run_ctx
    out_path = pdir / "runs" / run_id / "outputs" / f"{CLASSIFY_ID}.parquet"
    grown = pd.concat([pd.read_parquet(out_path)] * 2, ignore_index=True)
    grown.to_parquet(out_path, index=False)
    html = _panel(run_id, CLASSIFY_ID)
    assert "output as a diff against its input" not in html
    assert "output data" in html
