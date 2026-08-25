"""The stage diff on both surfaces through the real runner — the panel's Data
pane and the full-rows page: a 1:1 stage (enrich included, against its subject)
reads as a diff against its input, a filter stage as one merged table with its
dropped rows in place, the full-rows page keeps its row numbers/expandable cells
and offers ?raw=1, and an unverifiable alignment falls back to plain output."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

import app.services.workspace as workspace
import app.web.loading as loading
from app.main import app
from app.runtime.lineage_sidecar import resolve_lineage_sidecar_path
from app.runtime.runner import execute_run
from app.services import versioning
from app.services import project as project_service
from conftest import pinned_stages, reads_of
from stage_seed import add_stage

client = TestClient(app)

PROJECT = "stage_diff_panel"
LOAD_ID = "load"
CLASSIFY_ID = "classify"
KEEP_ID = "keep"
ROUTES_ID = "routes"
ROUTE_ID = "route"

_LOAD_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                            {"name": "val", "type": "int", "nullable": True},
                            {"name": "junk", "type": "str", "nullable": True}]}
_CLASSIFY_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                                {"name": "val", "type": "int", "nullable": True},
                                {"name": "junk", "type": "str", "nullable": True},
                                {"name": "label", "type": "str", "nullable": True}]}

# Uppercases `name` where val > 1 (a changed cell) and adds `label` (an added
# column), so the classify diff has one of each to show. It carries `junk`
# through because it must: an `extends` signature flows every anchor column, so
# a row-mapped stage cannot drop one — that is python_frame_function's job.
_CLASSIFY_CODE = (
    "def transform(row):\n"
    "    name = row['name'].upper() if row['val'] > 1 else row['name']\n"
    "    return {'name': name, 'val': row['val'], 'junk': row['junk'],\n"
    "            'label': 'big' if row['val'] > 1 else 'small'}\n"
)

_KEEP_CODE = (
    "def should_include(row):\n"
    "    return row['val'] != 2\n"
)

_ROUTES_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                              {"name": "route", "type": "str", "nullable": True}]}
# The enrich brings `route`; every subject column flows through untouched.
_ROUTE_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                             {"name": "val", "type": "int", "nullable": True},
                             {"name": "label", "type": "str", "nullable": True},
                             {"name": "route", "type": "str", "nullable": True}]}


def _seed_workflow(pdir: Path, data_path: Path, routes_path: Path) -> None:
    pdir.mkdir(parents=True, exist_ok=True)
    stages: list[tuple[str, dict]] = [
        ("01_load.json", {
            "id": LOAD_ID, "description": "Load rows", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(data_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _LOAD_SCHEMA["columns"]},
        }),
        ("02_classify.json", {
            "id": CLASSIFY_ID, "description": "Classify", "type": "python_row_function",
            "inputs": [{"id": LOAD_ID}],
            "function": {"kind": "inline", "code": _CLASSIFY_CODE},
            "signature": {"form": "extends",
                          "reads": reads_of(LOAD_ID, _LOAD_SCHEMA["columns"]),
                          "adds": [{"name": "label", "type": "str", "nullable": True}]},
        }),
        ("03_keep.json", {
            "id": KEEP_ID, "description": "Keep the small ones", "type": "filter_rows",
            "inputs": [{"id": CLASSIFY_ID}],
            "filter": {"code": _KEEP_CODE},
            "signature": {"form": "extends",
                          "reads": reads_of(CLASSIFY_ID, _CLASSIFY_SCHEMA["columns"])},
        }),
        ("04_routes.json", {
            "id": ROUTES_ID, "description": "Route reference", "type": "input_data",
            "connector": {"kind": "file",
                          "params": {"path": str(routes_path), "format": "csv"}},
            "signature": {"form": "replaces", "produces": _ROUTES_SCHEMA["columns"]},
        }),
        ("05_route.json", {
            "id": ROUTE_ID, "description": "Attach the route", "type": "enrich",
            "inputs": [{"id": CLASSIFY_ID},
                       {"id": ROUTES_ID}],
            "join": {"keys": [{"left": "name", "right": "name"}],
                     "enrich_with": {"route": "route"}},
            "signature": {
                "form": "extends",
                "reads": [
                    {
                        "input": "classify",
                        "columns": [{"name": "name", "type": "str", "nullable": True}],
                    },
                    {
                        "input": "routes",
                        "columns": [{"name": "name", "type": "str", "nullable": True}],
                    },
                ],
                "adds": [{"name": "route", "type": "str", "nullable": True}],
            },
        }),
    ]
    for filename, spec in stages:
        add_stage(pdir, spec)


@pytest.fixture()
def run_ctx(tmp_path: Path) -> tuple[Path, str]:
    pdir = tmp_path / PROJECT
    pdir.mkdir(parents=True, exist_ok=True)
    data = pdir / "rows.csv"
    pd.DataFrame({"name": ["alpha", "beta", "gamma"], "val": [1, 2, 1],
                  "junk": ["x", "y", "z"]}).to_csv(
        data, index=False)
    routes = pdir / "routes.csv"
    # Keyed on the names `classify` emits (it uppercases where val > 1).
    pd.DataFrame({"name": ["alpha", "BETA", "gamma"],
                  "route": ["north", "south", "east"]}).to_csv(routes, index=False)
    _seed_workflow(pdir, data, routes)
    workspace.set_projects_dir(tmp_path)
    version_id = project_service.save_working_copy_as_version(pdir.name, message="v1", reviewer="test").version_id
    versioning.publish_version(pdir.name, version_id, reviewer="test")
    run_id = str(execute_run(pdir / "runs", pdir.name, *pinned_stages(pdir))["run_id"])
    return pdir, run_id


def _panel(run_id: str, stage_id: str) -> str:
    response = client.get(f"/project/{PROJECT}/runs/{run_id}/stage/{stage_id}/partial")
    assert response.status_code == 200
    return response.text


def _diff_head(html: str) -> str:
    assert 'class="preview-head diff-head"' in html
    # The strip's own elements are spans, so the first </div> closes the head.
    return html.split('class="preview-head diff-head"')[1].split("</div>")[0]


def _unit_at(strip: str, frame_id: str) -> int:
    return strip.index(f">{frame_id}</code>")


def _rows_toolbar(html: str) -> str:
    assert 'class="rows-toolbar"' in html
    return html.split('class="rows-toolbar"')[1].split("</div>")[0]


def _output_unit(html: str) -> str:
    return _diff_head(html).split('class="diff-outputs"')[1]


def test_a_row_function_output_reads_as_a_diff_against_its_input(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, CLASSIFY_ID)
    # The added column is tinted AND carries the colour-free + on its header.
    assert "diff-col-new" in html
    assert '<span class="diff-mark">+</span><span class="diff-col-name">label</span>' in html
    assert 'title="added by this stage"' in html
    # The one uppercased name is a marked changed cell carrying its old value.
    assert "diff-cell-changed" in html
    assert "BETA" in html and "beta" in html
    # The diff table stands in for the plain output preview.
    assert "output data" not in html


def _diff_header(html: str) -> str:
    return html.split('class="preview-block stage-diff"')[1].split("</thead>")[0]


def test_the_columns_the_stage_wrote_are_drawn_at_the_left_edge(run_ctx) -> None:
    # The output frame puts `label` last, behind a horizontal scroll.
    _pdir, run_id = run_ctx
    header = _diff_header(_panel(run_id, CLASSIFY_ID))
    assert header.index(">label<") < header.index(">name<") < header.index(">junk<")


def test_the_raw_rows_view_leads_with_the_same_columns(run_ctx) -> None:
    # The plain table has no + mark to find an added column by.
    _pdir, run_id = run_ctx
    header = _rows_page(run_id, CLASSIFY_ID, "?raw=1").split("</thead>")[0]
    assert header.index(">label<") < header.index(">name<") < header.index(">junk<")


def test_the_rail_states_the_counts_of_what_the_stage_did_in_one_line(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, CLASSIFY_ID))
    # classify adds `label` and uppercases one name.
    assert ">+1 col · 1 cell changed</span>" in strip
    assert "diff-summary" not in strip


def test_neither_shape_carries_a_prose_block_the_table_already_says(run_ctx) -> None:
    _pdir, run_id = run_ctx
    for stage_id in (CLASSIFY_ID, ROUTE_ID, KEEP_ID):
        html = _panel(run_id, stage_id)
        assert "diff-summary" not in html
        assert "<caption>" not in html
        assert "added by this stage, carried by no input" not in html
        assert "every value carried through unchanged" not in html


def test_the_header_gives_every_frame_its_own_labelled_unit(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, ROUTE_ID))
    # One unit per frame — both inputs and the output — each naming its own part.
    assert strip.count('class="diff-frame ') == 3
    assert ">base input</span>" in strip
    assert ">reference input</span>" in strip
    assert ">output</span>" in strip
    # The input set stands before the arrow, the output after it.
    assert (_unit_at(strip, CLASSIFY_ID) < _unit_at(strip, ROUTES_ID)
            < strip.index("diff-arrow") < _unit_at(strip, ROUTE_ID))
    # Every unit carries its own pair of links, so no frame is a dead end.
    for frame_id in (CLASSIFY_ID, ROUTES_ID, ROUTE_ID):
        assert f'/stage/{frame_id}/rows?raw=1"' in strip
        assert f'/stage/{frame_id}/rows.csv"' in strip


def test_a_one_input_stage_names_its_only_input_without_a_base_marker(run_ctx) -> None:
    # With one input there is nothing to tell apart, so "base" would be noise.
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, CLASSIFY_ID))
    assert strip.count('class="diff-frame ') == 2
    assert ">input</span>" in strip and ">output</span>" in strip
    assert "base input" not in strip and "reference input" not in strip
    assert _unit_at(strip, LOAD_ID) < strip.index("diff-arrow") < _unit_at(strip, CLASSIFY_ID)
    for frame_id in (LOAD_ID, CLASSIFY_ID):
        assert f'/stage/{frame_id}/rows?raw=1"' in strip
        assert f'/stage/{frame_id}/rows.csv"' in strip


def test_a_second_input_lengthens_the_input_stack_without_moving_the_output(run_ctx) -> None:
    # app/static/node-detail.css holds the axis to one line, top-aligned.
    _pdir, run_id = run_ctx
    for stage_id, input_ids in ((CLASSIFY_ID, [LOAD_ID]),
                                (ROUTE_ID, [CLASSIFY_ID, ROUTES_ID])):
        strip = _diff_head(_panel(run_id, stage_id))
        stack = strip.split('class="diff-inputs"')[1].split('class="diff-rail"')[0]
        for input_id in input_ids:
            assert f">{input_id}</code>" in stack
        assert f">{stage_id}</code>" not in stack
        # The output unit is the LAST thing on the axis, after the rail.
        assert strip.index('class="diff-outputs"') > strip.index('class="diff-rail"')
        assert f">{stage_id}</code>" in strip.split('class="diff-outputs"')[1]


def test_the_bracket_appears_only_where_there_is_more_than_one_input(run_ctx) -> None:
    _pdir, run_id = run_ctx
    assert "diff-brace" not in _diff_head(_panel(run_id, CLASSIFY_ID))
    assert "diff-brace" in _diff_head(_panel(run_id, ROUTE_ID))


def test_every_frame_unit_carries_the_row_count_of_the_frame_it_names(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, ROUTE_ID))
    # 3 subject rows, 3 reference rows, 3 output rows — one count per unit.
    assert strip.count("3 rows") == 3


def test_an_unread_reference_frame_shows_no_row_count_rather_than_a_guess(run_ctx) -> None:
    pdir, run_id = run_ctx
    # The reference frame is not needed to build the diff, so its loss must cost
    # the count and nothing else — never a fabricated or defaulted number.
    (pdir / "runs" / run_id / "outputs" / f"{ROUTES_ID}.parquet").unlink()
    strip = _diff_head(_panel(run_id, ROUTE_ID))
    assert ">reference input</span>" in strip
    assert f'/stage/{ROUTES_ID}/rows?raw=1"' in strip
    assert strip.count("3 rows") == 2  # the subject and the output, not the reference
    assert "0 rows" not in strip


def test_the_filter_header_folds_its_counts_into_the_frames_and_the_rail(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, KEEP_ID))
    # 3 in, 1 dropped by the stage, 2 out — the shape of the transform, in place.
    assert "3 rows" in strip and "2 rows" in strip
    assert ">−1 row</span>" in strip
    assert "kept 2 of 3" not in strip  # the old prose line is gone, not doubled


def test_the_filter_rail_reports_no_metric_the_filter_never_measured(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_panel(run_id, KEEP_ID))
    assert "cells changed" not in strip and "cols" not in strip


def test_both_shapes_put_their_count_labels_in_the_same_slot(run_ctx) -> None:
    _pdir, run_id = run_ctx
    for stage_id in (CLASSIFY_ID, ROUTE_ID, KEEP_ID):
        strip = _diff_head(_panel(run_id, stage_id))
        assert strip.count('class="diff-rail"') == 1
        assert strip.count('class="diff-count-labels"') == 1
        # …and the shape-specific sentences that used to sit there are gone.
        assert "output as a diff against its input" not in strip
        assert "dropped rows shown in place" not in strip


def test_the_full_rows_page_lays_the_frames_out_the_same_way(run_ctx) -> None:
    _pdir, run_id = run_ctx
    strip = _diff_head(_rows_page(run_id, ROUTE_ID))
    assert strip.count('class="diff-frame ') == 3
    assert ">base input</span>" in strip and ">reference input</span>" in strip
    assert f'/stage/{ROUTES_ID}/rows.csv"' in strip


def test_a_filter_stage_shows_its_dropped_rows_in_place(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, KEEP_ID)
    # ONE merged table: kept rows with lineage links, the dropped row inline,
    # tinted, carrying its input ordinal — no second table to reconcile.
    assert "diff-row-dropped" in html
    # The cell says what happened, not which ordinal it happened to.
    assert "Dropped row" in html
    assert "input row 1" not in html
    assert "BETA" in html  # the dropped row's content is visible
    assert "/trace/view" in html  # a kept row still opens its own lineage
    assert "output data" not in html  # the merged table IS the output view


def test_an_enrich_reads_as_a_diff_against_its_subject_input(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, ROUTE_ID)
    # The subject input (inputs[0]) is the BASE unit — never the reference frame.
    strip = _diff_head(html)
    assert (strip.index(">base input</span>") < _unit_at(strip, CLASSIFY_ID)
            < strip.index(">reference input</span>") < _unit_at(strip, ROUTES_ID))
    # The enrich adds `route` and touches nothing else: enrich_with never drops a
    # subject column.
    assert ">+1 col · 0 cells changed</span>" in strip
    assert "diff-col-new" in html and "north" in html


def test_the_tab_strip_replaces_inputs_and_outputs(run_ctx) -> None:
    _pdir, run_id = run_ctx
    for stage_id in (CLASSIFY_ID, LOAD_ID):  # in-scope and out-of-scope alike
        html = _panel(run_id, stage_id)
        assert 'data-tab="data"' in html and 'data-tab="transform"' in html
        assert 'data-tab="inputs"' not in html and 'data-tab="outputs"' not in html
        assert 'data-pane="data"' in html and 'data-pane="schema"' in html
        assert "run-inputs" not in html and "run-outputs" not in html
        assert "schema-inputs" not in html and "schema-outputs" not in html


def test_the_data_pane_keeps_the_input_rows_without_the_picker(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, CLASSIFY_ID)
    assert "data-inputs" in html
    assert 'class="row-pick"' not in html and "scratch-run" not in html
    assert f"/stage/{CLASSIFY_ID}/simulate" in html


def test_an_input_stage_keeps_the_plain_output_view_in_data(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _panel(run_id, LOAD_ID)
    assert "stage-diff" not in html
    assert "output data" in html
    # Its schema pane carries the output schema the old Outputs tab showed.
    assert "output schema" in html


def test_a_filter_missing_its_sidecar_falls_back_to_the_plain_pane(run_ctx) -> None:
    pdir, run_id = run_ctx
    resolve_lineage_sidecar_path(pdir / "runs" / run_id, KEEP_ID).unlink()
    html = _panel(run_id, KEEP_ID)
    assert "stage-diff" not in html
    assert "output data" in html


def test_a_row_count_mismatch_falls_back_to_the_plain_pane(run_ctx) -> None:
    pdir, run_id = run_ctx
    out_path = pdir / "runs" / run_id / "outputs" / f"{CLASSIFY_ID}.parquet"
    grown = pd.concat([pd.read_parquet(out_path)] * 2, ignore_index=True)
    grown.to_parquet(out_path, index=False)
    html = _panel(run_id, CLASSIFY_ID)
    assert "stage-diff" not in html
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
    assert "stage-diff" in html
    assert "diff-col-new" in html
    assert '<span class="diff-mark">+</span><span class="diff-col-name">label</span>' in html
    assert "diff-cell-changed" in html and "BETA" in html


def test_the_full_rows_diff_keeps_the_row_numbers_and_expandable_cells(run_ctx) -> None:
    # The shared partial takes the row numbers and the click-to-expand cells as flags.
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "diff-cell-changed" in html  # it IS the diff table these belong to
    assert '<th class="row-num">#</th>' in html
    assert '<td class="row-num muted">1</td>' in html
    assert "cell-clip" in html
    assert "Click any clipped cell to expand it" in html


def test_the_diff_page_leaves_the_view_toggle_and_the_csv_to_the_header(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    toolbar = _rows_toolbar(html)
    assert "view-note" not in toolbar
    assert "Download full CSV" not in toolbar
    unit = _output_unit(html)
    assert f'/stage/{CLASSIFY_ID}/rows?raw=1"' in unit
    assert f'/stage/{CLASSIFY_ID}/rows.csv"' in unit
    # What is left still has content, so the strip is no empty gap.
    assert "Click any clipped cell to expand it" in toolbar


def test_the_capped_diff_warning_sends_the_reader_to_a_link_that_exists(run_ctx, monkeypatch) -> None:
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 2)
    _pdir, run_id = run_ctx
    for stage_id in (CLASSIFY_ID, KEEP_ID):
        html = _rows_page(run_id, stage_id)
        toolbar = _rows_toolbar(html)
        assert "the output frame's ⬇ CSV link above" in toolbar
        assert "⬇ CSV</a>" in _output_unit(html)


def test_raw_1_forces_the_plain_table_and_says_which_view_it_is(run_ctx) -> None:
    # This branch renders no header rail, so the toolbar is the only route out.
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID, "?raw=1")
    assert "stage-diff" not in html
    toolbar = _rows_toolbar(html)
    assert "raw output table" in toolbar
    # …and the way to the diff is one click away, stated in words.
    assert f'/stage/{CLASSIFY_ID}/rows"' in toolbar
    assert "as a diff against its input" in toolbar
    assert "Download full CSV" in toolbar
    assert f'/stage/{CLASSIFY_ID}/rows.csv"' in toolbar


def test_a_stage_with_no_diff_offers_no_diff_view(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, LOAD_ID)
    assert "stage-diff" not in html
    toolbar = _rows_toolbar(html)
    assert "raw output table" in toolbar
    assert "as a diff against its input" not in toolbar
    # No header exists to carry it, so the download button stays here too.
    assert "Download full CSV" in toolbar


def test_a_filter_full_rows_page_shows_its_dropped_rows_in_place(run_ctx) -> None:
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, KEEP_ID)
    assert "diff-row-dropped" in html and "BETA" in html
    assert '<th class="row-num">#</th>' in html


def test_the_full_rows_diff_is_windowed_by_the_table_row_cap(run_ctx, monkeypatch) -> None:
    # The budget is the page's own MAX_TABLE_ROWS, not the panel's preview window.
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 2)
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, CLASSIFY_ID)
    assert "diff-cell-changed" in html  # the windowed table is the DIFF's
    assert "alpha" in html and "gamma" not in html
    assert "Showing first 2 of 3 rows" in html


def test_a_capped_filter_page_counts_input_rows_not_output_rows(run_ctx, monkeypatch) -> None:
    # keep's output has 2 rows and its input 3; the table drawn is over the 3.
    monkeypatch.setattr(loading, "MAX_TABLE_ROWS", 2)
    _pdir, run_id = run_ctx
    html = _rows_page(run_id, KEEP_ID)
    assert "Showing first 2 of 3 input rows" in html


def test_every_frame_unit_links_the_raw_view_not_another_diff(run_ctx) -> None:
    # Every input plus the output, on a one-input and a two-input stage alike.
    _pdir, run_id = run_ctx
    for stage_id, linked in ((CLASSIFY_ID, [LOAD_ID, CLASSIFY_ID]),
                             (ROUTE_ID, [CLASSIFY_ID, ROUTES_ID, ROUTE_ID])):
        strip = _diff_head(_panel(run_id, stage_id))
        for linked_id in linked:
            assert f'/stage/{linked_id}/rows?raw=1"' in strip
        assert '/rows"' not in strip  # no link in the strip serves a diff
