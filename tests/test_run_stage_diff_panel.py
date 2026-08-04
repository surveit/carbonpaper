"""The stage diff on both surfaces through the real runner — the panel's Data
pane and the full-rows page: a 1:1 stage (enrich included, against its subject)
reads as a diff against its input, a filter stage as one merged table with its
dropped rows in place, the full-rows page keeps its row numbers/expandable cells
and offers ?raw=1, and an unverifiable alignment falls back to plain output."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
import app.web.loading as loading
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
ROUTES_ID = "routes"
ROUTE_ID = "route"

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

_ROUTES_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                              {"name": "route", "type": "str", "nullable": True}]}
# `select` keeps name/label/route, so the enrich ADDS `route` and DROPS `val`.
_ROUTE_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                             {"name": "label", "type": "str", "nullable": True},
                             {"name": "route", "type": "str", "nullable": True}]}


def _seed_compiled(pdir: Path, data_path: Path, routes_path: Path) -> None:
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
        ("04_routes.json", {
            "id": ROUTES_ID, "name": "Route reference", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(routes_path), "format": "csv"}},
            "output_schema": _ROUTES_SCHEMA,
        }),
        ("05_route.json", {
            "id": ROUTE_ID, "name": "Attach the route", "type": "enrich",
            "inputs": [{"id": CLASSIFY_ID, "schema": _CLASSIFY_SCHEMA},
                       {"id": ROUTES_ID, "schema": _ROUTES_SCHEMA}],
            "join": {"keys": [{"left": "name", "right": "name"}],
                     "select": ["name", "label", "route"]},
            "output_schema": _ROUTE_SCHEMA,
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
    routes = pdir / "routes.csv"
    # Keyed on the names `classify` emits (it uppercases where val > 1).
    pd.DataFrame({"name": ["alpha", "BETA", "gamma"],
                  "route": ["north", "south", "east"]}).to_csv(routes, index=False)
    _seed_compiled(pdir, data, routes)
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
    assert "raw frames:" in html and "output <code>classify</code>" in html
    assert f"/stage/{LOAD_ID}/rows.csv" in html
    assert f"/stage/{CLASSIFY_ID}/rows.csv" in html
    # With one input there is nothing to tell apart, so no base marker.
    assert "base input" not in html


def test_the_diff_header_links_every_input_of_a_two_input_stage(run_ctx) -> None:
    # The enrich diffs against its subject, but the REFERENCE frame is where the
    # added columns came from — link it too, with the base one identifiable.
    _pdir, run_id = run_ctx
    html = _panel(run_id, ROUTE_ID)
    assert f"base input <code>{CLASSIFY_ID}</code>" in html
    assert f"input <code>{ROUTES_ID}</code>" in html
    assert f"/stage/{CLASSIFY_ID}/rows.csv" in html
    assert f"/stage/{ROUTES_ID}/rows.csv" in html


def test_a_filter_stage_shows_its_dropped_rows_in_place(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, KEEP_ID)
    # ONE merged table: kept rows with lineage links, the dropped row inline,
    # tinted, carrying its input ordinal — no second table to reconcile.
    assert "dropped rows shown in place" in html
    assert "diff-row-dropped" in html
    # The cell says what happened, not which ordinal it happened to.
    assert "Dropped row" in html
    assert "input row 1" not in html
    assert "BETA" in html  # the dropped row's content is visible
    assert "View lineage" in html  # kept rows keep their lineage links
    assert "output data" not in html  # the merged table IS the output view


def test_an_enrich_reads_as_a_diff_against_its_subject_input(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, ROUTE_ID)
    # The subject input (inputs[0]) heads the diff — never the reference frame.
    assert f"<code>{CLASSIFY_ID}</code> → <code>{ROUTE_ID}</code>" in html
    assert "output as a diff against its input" in html
    assert "diff-col-new" in html and "<code>route</code>" in html
    assert "north" in html


def test_a_dropped_column_is_drawn_in_the_table_carrying_its_input_value(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, ROUTE_ID)
    # `val` left the output via join.select; the reader still sees what it held.
    assert "diff-col-dropped" in html
    assert "dropped by this stage" in html  # the caption names it in words
    assert "<code>val</code>" in html
    # The input is the base, so `val` holds its input position — ahead of the
    # added `route` — rather than being exiled to the end of the table.
    assert html.index('class="diff-col-dropped">val') < html.index('class="diff-col-new">route')


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


# ─── the full-rows page: the same diff over its own, much larger budget ──────

def _rows_page(run_id: str, stage_id: str, query: str = "") -> str:
    response = client.get(
        f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/rows{query}")
    assert response.status_code == 200
    return response.text


def test_the_full_rows_page_reads_as_a_diff_by_default(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "output as a diff against its input" in html
    assert "diff-col-new" in html and "<code>label</code>" in html
    assert "diff-cell-changed" in html and "BETA" in html


def test_the_full_rows_diff_keeps_the_row_numbers_and_expandable_cells(run_ctx) -> None:
    """What this page does better than the panel survives the diff."""
    # Row numbers and the click-to-expand (title-hover) cell treatment are the
    # page's own; the shared partial takes them as flags rather than being forked.
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "diff-cell-changed" in html  # it IS the diff table these belong to
    assert '<th class="row-num">#</th>' in html
    assert '<td class="row-num muted">1</td>' in html
    assert "cell-clip" in html
    assert "Click any clipped cell to expand it" in html


def test_the_diff_page_names_its_view_and_links_the_raw_one(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "diff against its input" in html
    assert f"/stage/{CLASSIFY_ID}/rows?raw=1" in html


def test_raw_1_forces_the_plain_table_and_says_which_view_it_is(run_ctx) -> None:
    """A reader arriving from a raw-frames link must not be handed an annotated table."""
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID, "?raw=1")
    assert "stage-diff" not in html
    assert "raw output table" in html
    # …and the way to the diff is one click away, stated in words.
    assert f'/stage/{CLASSIFY_ID}/rows"' in html
    assert "as a diff against its input" in html


def test_a_stage_with_no_diff_offers_no_diff_view(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, LOAD_ID)
    assert "stage-diff" not in html
    assert "raw output table" in html
    assert "as a diff against its input" not in html


def test_a_filter_full_rows_page_shows_its_dropped_rows_in_place(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, KEEP_ID)
    assert "dropped rows shown in place" in html
    assert "diff-row-dropped" in html and "BETA" in html
    assert '<th class="row-num">#</th>' in html


def test_the_full_rows_diff_is_windowed_by_the_table_row_cap(run_ctx, monkeypatch) -> None:
    # The budget is the page's own MAX_TABLE_ROWS, not the panel's five.
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 2)
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "diff-cell-changed" in html  # the windowed table is the DIFF's
    assert "alpha" in html and "gamma" not in html
    assert "Showing first 2 of 3 rows" in html


def test_a_capped_filter_page_counts_input_rows_not_output_rows(run_ctx, monkeypatch) -> None:
    """The filter table is over INPUT rows, so the cap warning must say so."""
    # keep's output has 2 rows and its input 3; a warning that said "of 2 rows"
    # would be a false count for the table actually drawn.
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 2)
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, KEEP_ID)
    assert "Showing first 2 of 3 input rows" in html


def test_the_raw_frames_strip_links_the_raw_view_not_another_diff(run_ctx) -> None:
    """The strip is labelled "raw frames"; every link in it must reach a raw table."""
    # Every input plus the output, on a one-input and a two-input stage alike.
    _pdir, run_id = run_ctx
    for stage_id, linked in ((CLASSIFY_ID, [LOAD_ID, CLASSIFY_ID]),
                             (ROUTE_ID, [CLASSIFY_ID, ROUTES_ID, ROUTE_ID])):
        strip = _panel(run_id, stage_id).split("raw frames:")[1].split("</span>")[0]
        for linked_id in linked:
            assert f'/stage/{linked_id}/rows?raw=1"' in strip
        assert '/rows"' not in strip  # no link in the strip serves a diff
