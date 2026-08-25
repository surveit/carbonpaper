"""The Data-pane stage diff (app.web.stage_diff): the INPUT frame as the column
spine with added, dropped and changed columns painted over it, for 1:1 stages
including enrich (against its subject input); the merged kept-and-dropped table
for filter_rows read off the lineage sidecar; None for every out-of-scope stage
type and wherever the alignment cannot be verified."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models.stage import Stage, StageType, is_grain_and_order_preserving, parse_stage
from app.runtime.lineage_sidecar import resolve_lineage_sidecar_path
from app.runtime.lineage import (
    TRACE_SOURCE_ROW_KEY,
    TRACE_SOURCE_STAGE_KEY,
)
from app.web.loading import PREVIEW_ROWS_SHOWN, load_output_preview
from app.web.diff_state import CellDiffState, ColumnDiffState
from app.web.stage_diff import (
    _NO_ALIGNED_DIFF,
    BASE_INPUT_ROLE,
    FILTER_ROWS_KIND,
    REFERENCE_INPUT_ROLE,
    ROW_ALIGNED_KIND,
    ROW_ALIGNED_TYPES,
    SOLE_INPUT_ROLE,
    FilterRowsDiff,
    RowAlignedDiff,
    build_stage_diff,
)
from conftest import place_stage, reads_of

LOAD_ID = "load"
_LOAD_PATH = f"outputs/{LOAD_ID}.parquet"

_IN_COLUMNS = [
    {"name": "name", "type": "str", "nullable": True},
    {"name": "val", "type": "int", "nullable": True},
]
_OUT_COLUMNS = _IN_COLUMNS + [{"name": "label", "type": "str", "nullable": True}]


def _row_stage(output_columns: list[dict] | None = None) -> Stage:
    return parse_stage({
        "id": "classify", "description": "Classify", "type": "python_row_function",
        "inputs": [{"id": LOAD_ID}],
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return row\n"},
        "signature": {"form": "extends", "adds": _added(output_columns or _OUT_COLUMNS,
                                                       _IN_COLUMNS)},
    })


def _added(output_columns, edge_columns):
    flowing = {c["name"] for c in edge_columns}
    return [c for c in output_columns if c["name"] not in flowing]


_REF_COLUMNS = [
    {"name": "name", "type": "str", "nullable": True},
    {"name": "extra", "type": "str", "nullable": True},
]
_ENRICHED_COLUMNS = _IN_COLUMNS + [{"name": "extra", "type": "str", "nullable": True}]
REF_ID = "ref"
_REF_PATH = f"outputs/{REF_ID}.parquet"


def _join_stage(stage_type: str, output_columns: list[dict] | None = None) -> Stage:
    return parse_stage({
        "id": "route", "description": "Route", "type": stage_type,
        "inputs": [{"id": LOAD_ID},
                   {"id": REF_ID}],
        "join": {"keys": [{"left": "name", "right": "name"}], "enrich_with": {"extra": "extra"}},
        "signature": {
            "form": "extends",
            "reads": [{"input": LOAD_ID, "columns": [
                          {"name": "name", "type": "str", "nullable": True}]},
                      {"input": REF_ID, "columns": [
                          {"name": "name", "type": "str", "nullable": True}]}],
            "adds": _added(output_columns or _ENRICHED_COLUMNS, _IN_COLUMNS)},
    })


def _filter_stage() -> Stage:
    return parse_stage({
        "id": "keep", "description": "Keep", "type": "filter_rows",
        "inputs": [{"id": LOAD_ID}],
        "filter": {"code": "def should_include(row):\n    return row['val'] is not None\n"},
        "signature": {"form": "extends", "reads": reads_of(LOAD_ID, _IN_COLUMNS)},
    })


def _starlark_filter_stage() -> Stage:
    return parse_stage({
        "id": "keep", "description": "Keep", "type": "starlark_filter_rows",
        "inputs": [{"id": LOAD_ID}],
        "starlark_filter": {
            "code": "def should_include(row):\n    return row['val'] != None\n"},
        "signature": {"form": "extends", "reads": reads_of(LOAD_ID, _IN_COLUMNS)},
    })


def _write_output(run_dir: Path, stage_id: str, df: pd.DataFrame) -> str:
    (run_dir / "outputs").mkdir(parents=True, exist_ok=True)
    rel = f"outputs/{stage_id}.parquet"
    df.to_parquet(run_dir / rel, index=False)
    return rel


def _write_lineage(run_dir: Path, stage_id: str, kept: list[int]) -> None:
    pd.DataFrame({
        TRACE_SOURCE_STAGE_KEY: [LOAD_ID] * len(kept),
        TRACE_SOURCE_ROW_KEY: kept,
    }).to_parquet(resolve_lineage_sidecar_path(run_dir, stage_id), index=False)


def _diff(run_dir: Path, stage_def: Stage, out_rel: str):
    return build_stage_diff(place_stage(stage_def), run_dir, out_rel, {LOAD_ID: _LOAD_PATH})


def _numbered_frame(rows: int) -> pd.DataFrame:
    return pd.DataFrame({"name": [f"r{i}" for i in range(rows)], "val": list(range(rows))})


# ─── row-aligned: added columns, changed cells, unchanged passthrough ────────

def test_a_column_the_stage_added_is_named_and_marked(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a", "b"], "val": [1, 2], "label": ["x", "y"]}))

    diff = _diff(tmp_path, _row_stage(), out_rel)

    assert diff is not None and diff.kind == ROW_ALIGNED_KIND
    assert diff.added_column_names == ["label"]
    label_cells = [row[0] for row in diff.rows]  # the stage's own column leads
    assert all(cell.state is CellDiffState.added for cell in label_cells)
    assert diff.changed_cells_total == 0


def test_changed_cells_are_counted_over_the_whole_frame_and_marked(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"val": [1, 2, 3], "name": ["a", "b", "c"]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"val": [1, 2, 3], "name": ["a", "B", "C"]}))

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert diff is not None
    name_column = next(c for c in diff.columns if c.name == "name")
    assert name_column.changed_cells == 2
    assert diff.changed_cells_total == 2
    assert [column.name for column in diff.columns] == ["name", "val"]
    changed_cell = diff.rows[1][0]
    assert changed_cell.state is CellDiffState.changed
    assert changed_cell.was == "b" and changed_cell.text == "B"
    untouched_cell = diff.rows[0][0]
    assert untouched_cell.state is CellDiffState.carried and untouched_cell.was is None


def test_an_unchanged_passthrough_reports_every_value_carried(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "classify", frame.copy())

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert diff is not None
    assert diff.changed_cells_total == 0
    assert diff.added_column_names == [] and diff.removed_column_names == []
    assert all(cell.state is CellDiffState.carried for row in diff.rows for cell in row)
    assert all(c.state is ColumnDiffState.carried for c in diff.columns)


def test_the_columns_the_stage_wrote_lead_the_input_frame_spine(
    tmp_path: Path,
) -> None:
    # Here `val` is dropped and `label` is added.
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a"], "label": ["x"]}))
    out_columns = [{"name": "name", "type": "str", "nullable": True},
                   {"name": "label", "type": "str", "nullable": True}]

    diff = _diff(tmp_path, _row_stage(out_columns), out_rel)

    assert diff is not None
    # Touched columns lead; untouched input columns retain their relative order.
    assert [(c.name, c.state) for c in diff.columns] == [
        ("label", ColumnDiffState.added),
        ("val", ColumnDiffState.dropped),
        ("name", ColumnDiffState.carried)]
    assert [cell.state for cell in diff.rows[0]] == [
        CellDiffState.added, CellDiffState.dropped, CellDiffState.carried]
    # The dropped column carries the INPUT value, so the reader sees what was lost.
    assert [cell.text for cell in diff.rows[0]] == ["x", "1", "a"]
    assert diff.removed_column_names == ["val"] and diff.added_column_names == ["label"]
    assert diff.changed_cells_total == 0


def test_an_llm_transform_is_admitted_to_the_row_aligned_diff(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "judge", "description": "Judge", "type": "llm_transform",
        "inputs": [{"id": LOAD_ID}],
        "llm": {"prompt_data_template": "{name}"},
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "name", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "label", "type": "str", "nullable": True}],
        },
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "judge", pd.DataFrame(
        {"name": ["a"], "val": [1], "label": ["x"]}))

    diff = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(diff, RowAlignedDiff) and diff.kind == ROW_ALIGNED_KIND
    assert diff.added_column_names == ["label"]


def test_a_starlark_row_function_is_admitted_to_the_row_aligned_diff(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "classify", "description": "Classify", "type": "starlark_row_function",
        "inputs": [{"id": LOAD_ID}],
        "starlark": {"code": "def transform(row):\n    return row"},
        "signature": {
            "form": "extends",
            "reads": [
                {
                    "input": "load",
                    "columns": [{"name": "name", "type": "str", "nullable": True}],
                },
            ],
            "adds": [{"name": "basis", "type": "str", "nullable": True}],
        },
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a"], "val": [1], "basis": ["x"]}))

    diff = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(diff, RowAlignedDiff) and diff.kind == ROW_ALIGNED_KIND
    assert diff.added_column_names == ["basis"]


def test_a_review_queue_shows_the_human_answer_beside_what_it_answered(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "gate", "description": "Gate", "type": "human_review_queue",
        "inputs": [{"id": LOAD_ID}],
        "queue": {
            "reviewed_columns": {"name": "reviewed_name"},
            "verdict_column": "verdict",
            "reviewer_column": "reviewer",
            "reviewed_at_column": "reviewed_at",
        },
        "signature": {
            "form": "extends",
            "reads": reads_of(LOAD_ID, _IN_COLUMNS),
            "adds": [{"name": "reviewed_name", "type": "str", "nullable": True},
                     {"name": "verdict", "type": "str", "nullable": True},
                     {"name": "reviewer", "type": "str", "nullable": True},
                     {"name": "reviewed_at", "type": "str", "nullable": True}],
        },
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "gate", pd.DataFrame({
        "name": ["a"], "val": [1], "reviewed_name": ["A, corrected"],
        "verdict": ["approve"], "reviewer": ["shuhan"], "reviewed_at": ["2026-08-06"]}))

    diff = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(diff, RowAlignedDiff)
    assert diff.added_column_names == [
        "reviewed_name", "verdict", "reviewer", "reviewed_at"]
    assert diff.changed_cells_total == 0
    judged = next(c for c in diff.columns if c.name == "name")
    assert judged.state is ColumnDiffState.carried


def test_every_grain_preserving_type_gets_a_diff_unless_it_has_nothing_to_compare() -> None:
    for stage_type in StageType:
        if not is_grain_and_order_preserving(stage_type):
            continue
        assert (stage_type in ROW_ALIGNED_TYPES) != (stage_type in _NO_ALIGNED_DIFF), (
            f"{stage_type.value} is grain-and-order-preserving, so it is either "
            f"covered by the aligned diff or listed in _NO_ALIGNED_DIFF with a reason"
        )


def test_the_only_excluded_type_is_the_one_with_no_input_to_compare() -> None:
    # Growing this list means a type stopped getting a diff — a decision, not a refactor.
    assert _NO_ALIGNED_DIFF == {StageType.input_data}


# ─── enrich: the row-aligned diff against its SUBJECT input ──────────────────

def _enrich_frames(tmp_path: Path) -> str:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    _write_output(tmp_path, REF_ID, pd.DataFrame({"name": ["a", "b"], "extra": ["p", "q"]}))
    return _write_output(tmp_path, "route", pd.DataFrame(
        {"name": ["a", "b"], "val": [1, 2], "extra": ["p", "q"]}))


def _join_diff(tmp_path: Path, stage_def: Stage, out_rel: str):
    return build_stage_diff(place_stage(stage_def), tmp_path, out_rel,
                            {LOAD_ID: _LOAD_PATH, REF_ID: _REF_PATH})


def test_an_enrich_diffs_against_its_subject_input_not_its_reference(tmp_path: Path) -> None:
    # enrich is a left merge pandas VERIFIES is m:1, so every subject row survives.
    out_rel = _enrich_frames(tmp_path)

    diff = _join_diff(tmp_path, _join_stage("enrich"), out_rel)

    assert isinstance(diff, RowAlignedDiff) and diff.kind == ROW_ALIGNED_KIND
    # Both inputs are named, the SUBJECT first and as the base: the header links
    # the reference frame too, so the reader can reach where `extra` came from.
    assert [(f.stage_id, f.role, f.rows_total) for f in diff.inputs] == [
        (LOAD_ID, BASE_INPUT_ROLE, 2), (REF_ID, REFERENCE_INPUT_ROLE, 2)]
    assert diff.added_column_names == ["extra"]
    assert diff.changed_cells_total == 0


def test_a_reference_frame_that_will_not_read_is_listed_without_a_row_count(
    tmp_path: Path,
) -> None:
    # The diff never reads the reference frame, so its loss costs the count alone.
    out_rel = _enrich_frames(tmp_path)
    (tmp_path / _REF_PATH).unlink()

    diff = _join_diff(tmp_path, _join_stage("enrich"), out_rel)

    assert isinstance(diff, RowAlignedDiff)
    assert [(f.stage_id, f.rows_total) for f in diff.inputs] == [(LOAD_ID, 2), (REF_ID, None)]


def test_an_expand_gets_no_diff(tmp_path: Path) -> None:
    # expand is m:n: a matching row count is coincidence, not a contract.
    out_rel = _enrich_frames(tmp_path)

    assert _join_diff(tmp_path, _join_stage("expand"), out_rel) is None


def test_an_enrich_that_did_not_come_out_one_to_one_yields_no_diff(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    _write_output(tmp_path, REF_ID, pd.DataFrame({"name": ["a"], "extra": ["p"]}))
    out_rel = _write_output(tmp_path, "route", pd.DataFrame(
        {"name": ["a"], "val": [1], "extra": ["p"]}))

    assert _join_diff(tmp_path, _join_stage("enrich"), out_rel) is None


def test_an_enrich_that_dropped_a_subject_column_shows_it_carrying_the_input_value(
    tmp_path: Path,
) -> None:
    # The diff reads frames, not config: a column missing from the output still shows.
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    _write_output(tmp_path, REF_ID, pd.DataFrame({"name": ["a", "b"], "extra": ["p", "q"]}))
    out_rel = _write_output(tmp_path, "route", pd.DataFrame(
        {"name": ["a", "b"], "extra": ["p", "q"]}))
    stage = _join_stage("enrich", _REF_COLUMNS)

    diff = _join_diff(tmp_path, stage, out_rel)

    assert diff is not None
    assert diff.removed_column_names == ["val"]
    assert [(c.name, c.state) for c in diff.columns] == [
        ("extra", ColumnDiffState.added),
        ("val", ColumnDiffState.dropped),
        ("name", ColumnDiffState.carried)]
    dropped_cells = [row[1] for row in diff.rows]
    assert [cell.text for cell in dropped_cells] == ["1", "2"]
    assert all(cell.state is CellDiffState.dropped for cell in dropped_cells)


# ─── filter_rows: one merged table, dropped rows in place ────────────────────

def test_filter_rows_merges_kept_and_dropped_rows_in_input_order(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "c", "d"], "val": [1, 2, 3, 4]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame(
        {"name": ["a", "c"], "val": [1, 3]}))
    _write_lineage(tmp_path, "keep", kept=[0, 2])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None and diff.kind == FILTER_ROWS_KIND
    # One input: it is the base without needing the word, and its count is the
    # input row total the merged table is drawn over.
    assert [(f.stage_id, f.role, f.rows_total) for f in diff.inputs] == [
        (LOAD_ID, SOLE_INPUT_ROLE, 4)]
    assert diff.dropped_total == 2 and diff.kept_total == 2 and diff.input_total == 4
    assert [row.input_ordinal for row in diff.rows] == [0, 1, 2, 3]
    assert [row.dropped for row in diff.rows] == [False, True, False, True]
    # A kept row carries its ordinal IN THE OUTPUT, which is what its lineage
    # link needs; a dropped row carries none.
    assert [row.output_ordinal for row in diff.rows] == [0, None, 1, None]
    assert diff.rows[1].cells == ["b", "2"]
    assert diff.dropped_beyond_window == 0


def test_filter_rows_that_dropped_nothing_still_gets_the_merged_table(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "keep", frame.copy())
    _write_lineage(tmp_path, "keep", kept=[0, 1])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None and diff.kind == FILTER_ROWS_KIND
    assert diff.dropped_total == 0
    assert all(not row.dropped for row in diff.rows)


def test_filter_rows_counts_the_drops_beyond_the_shown_window(tmp_path: Path) -> None:
    total = PREVIEW_ROWS_SHOWN + 3
    kept = list(range(total - 1))  # the LAST input row is dropped, past the window
    _write_output(tmp_path, LOAD_ID, _numbered_frame(total))
    out_rel = _write_output(tmp_path, "keep", _numbered_frame(total - 1))
    _write_lineage(tmp_path, "keep", kept=kept)

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None and diff.kind == FILTER_ROWS_KIND
    assert len(diff.rows) == PREVIEW_ROWS_SHOWN
    assert all(not row.dropped for row in diff.rows)
    assert diff.dropped_total == 1 and diff.dropped_beyond_window == 1


# ─── count_labels: one vocabulary, only the metrics the shape measured ───────

def test_a_row_aligned_count_labels_names_the_columns_and_the_changed_cells(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["A", "b"], "label": ["x", "y"]}))
    out_columns = [{"name": "name", "type": "str", "nullable": True},
                   {"name": "label", "type": "str", "nullable": True}]

    diff = _diff(tmp_path, _row_stage(out_columns), out_rel)

    assert diff is not None
    assert diff.count_labels == ["+1 col", "−1 col", "1 cell changed"]


def test_a_row_aligned_count_labels_states_the_zero_change_it_measured(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "classify", frame.copy())

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert diff is not None
    # Nothing moved, and the rail still says something true rather than nothing.
    assert diff.count_labels == ["0 cells changed"]


def test_a_filter_count_labels_reports_rows_and_never_a_metric_it_did_not_measure(
    tmp_path: Path,
) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "c", "d"], "val": [1, 2, 3, 4]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame({"name": ["a", "c"], "val": [1, 3]}))
    _write_lineage(tmp_path, "keep", kept=[0, 2])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None
    assert diff.count_labels == ["−2 rows"]
    assert not any("cell" in part or "col" in part for part in diff.count_labels)


def test_a_filter_that_dropped_nothing_says_so_rather_than_nothing(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "keep", frame.copy())
    _write_lineage(tmp_path, "keep", kept=[0, 1])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None
    assert diff.count_labels == ["0 rows dropped"]


def test_both_shapes_expose_the_output_row_count_under_one_name(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    aligned_rel = _write_output(tmp_path, "classify", frame.copy())
    kept_rel = _write_output(tmp_path, "keep", pd.DataFrame({"name": ["a"], "val": [1]}))
    _write_lineage(tmp_path, "keep", kept=[0])

    aligned = _diff(tmp_path, _row_stage(_IN_COLUMNS), aligned_rel)
    filtered = _diff(tmp_path, _filter_stage(), kept_rel)

    assert isinstance(aligned, RowAlignedDiff) and isinstance(filtered, FilterRowsDiff)
    assert aligned.output_rows == 2 and filtered.output_rows == 1


# ─── the row budget is a parameter, and each shape windows its own frame ─────

def test_the_stage_panels_default_window_draws_a_hundred_rows(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, _numbered_frame(120))
    out_rel = _write_output(tmp_path, "classify", _numbered_frame(120))

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert isinstance(diff, RowAlignedDiff)
    assert len(diff.rows) == 100 and diff.rows_total == 120


def test_a_diffed_and_an_undiffed_stage_draw_the_same_number_of_rows(tmp_path: Path) -> None:
    total = PREVIEW_ROWS_SHOWN + 20
    # The two paths render into the same tab and look alike, so a reader who saw
    # 100 rows under one stage and 5 under the next read it as a fact about the data.
    _write_output(tmp_path, LOAD_ID, _numbered_frame(total))
    out_rel = _write_output(tmp_path, "classify", _numbered_frame(total))

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)
    plain = load_output_preview(tmp_path, out_rel)

    assert isinstance(diff, RowAlignedDiff) and plain is not None
    assert len(diff.rows) == len(plain["preview"]) == PREVIEW_ROWS_SHOWN
    assert diff.rows_total == plain["rows_total"] == total


def test_the_row_budget_windows_the_output_frame_of_an_aligned_diff(tmp_path: Path) -> None:
    # The caller sets how many rows are drawn; the whole-frame counts do not move.
    total = PREVIEW_ROWS_SHOWN + 2
    _write_output(tmp_path, LOAD_ID, _numbered_frame(total))
    changed = _numbered_frame(total)
    changed["name"] = changed["name"].str.upper()
    out_rel = _write_output(tmp_path, "classify", changed)
    stage = _row_stage(_IN_COLUMNS)

    wide = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}, rows_shown=total)
    default = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(wide, RowAlignedDiff) and isinstance(default, RowAlignedDiff)
    assert len(wide.rows) == total and len(default.rows) == PREVIEW_ROWS_SHOWN
    assert wide.rows_total == default.rows_total == total
    assert wide.changed_cells_total == default.changed_cells_total == total


def test_the_row_budget_windows_the_input_frame_of_a_filter_diff(tmp_path: Path) -> None:
    # A filter's table is over its INPUT rows, so a budget of 8 draws all 8 of them.
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": list("abcdefgh"), "val": list(range(8))}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame(
        {"name": list("abcdefg"), "val": list(range(7))}))
    _write_lineage(tmp_path, "keep", kept=[0, 1, 2, 3, 4, 5, 6])  # dropped: ordinal 7
    stage = _filter_stage()

    diff = build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}, rows_shown=8)

    assert isinstance(diff, FilterRowsDiff) and diff.kind == FILTER_ROWS_KIND
    assert len(diff.rows) == 8
    assert diff.rows[7].dropped and diff.rows[7].cells == ["h", "7"]
    assert diff.dropped_total == 1 and diff.dropped_beyond_window == 0


# ─── out-of-scope stage types: no diff, ever ─────────────────────────────────

def test_a_frame_function_gets_no_diff_even_at_matching_row_counts(tmp_path: Path) -> None:
    # A frame function may reorder rows, so a positional diff would be fabricated.
    stage = parse_stage({
        "id": "reshape", "description": "Reshape", "type": "python_frame_function",
        "inputs": [{"id": LOAD_ID}],
        "function": {"kind": "inline",
                     "code": "def transform(df):\n    return df\n"},
        "signature": {
            "form": "replaces",
            "reads": [{"input": "load", "columns": _IN_COLUMNS}],
            "produces": _IN_COLUMNS,
        },
    })
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "reshape", frame.copy())

    assert build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}) is None


def test_a_union_gets_no_diff(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "both", "description": "Both", "type": "union",
        "inputs": [{"id": LOAD_ID},
                   {"id": "more"}],
        "union": {},
        "signature": {"form": "extends", "reads": [], "adds": [], "rewrites": []},
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "both", pd.DataFrame({"name": ["a"], "val": [1]}))

    assert build_stage_diff(place_stage(stage), tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}) is None


def test_a_missing_stage_definition_gets_no_diff(tmp_path: Path) -> None:
    assert build_stage_diff(None, tmp_path, "outputs/x.parquet", {}) is None


# ─── graceful fallback: unverifiable alignment yields None ───────────────────

def test_a_row_count_mismatch_yields_no_diff(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a"], "val": [1], "label": ["x"]}))

    assert _diff(tmp_path, _row_stage(), out_rel) is None


def test_a_missing_input_frame_yields_no_diff(tmp_path: Path) -> None:
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a"], "val": [1], "label": ["x"]}))

    assert _diff(tmp_path, _row_stage(), out_rel) is None


def test_a_filter_without_its_lineage_sidecar_yields_no_diff(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame({"name": ["a"], "val": [1]}))

    assert _diff(tmp_path, _filter_stage(), out_rel) is None


def test_a_filter_whose_sidecar_disagrees_with_its_output_yields_no_diff(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame({"name": ["a"], "val": [1]}))
    _write_lineage(tmp_path, "keep", kept=[0, 1])  # names 2 rows; the output has 1

    assert _diff(tmp_path, _filter_stage(), out_rel) is None


def test_a_filter_whose_sidecar_ordinals_do_not_increase_yields_no_diff(tmp_path: Path) -> None:
    # A filter emits a subsequence, so kept ordinals strictly increase.
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame({"name": ["b", "a"], "val": [2, 1]}))
    _write_lineage(tmp_path, "keep", kept=[1, 0])

    assert _diff(tmp_path, _filter_stage(), out_rel) is None


def test_the_sandboxed_filter_gets_the_same_dropped_rows_view(tmp_path: Path) -> None:
    """It had no diff at all until the type was wired into FILTER_TYPES."""
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "c", "d"], "val": [1, 2, 3, 4]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame(
        {"name": ["a", "c"], "val": [1, 3]}))
    _write_lineage(tmp_path, "keep", kept=[0, 2])

    diff = _diff(tmp_path, _starlark_filter_stage(), out_rel)

    assert diff is not None and diff.kind == FILTER_ROWS_KIND
    assert diff.dropped_total == 2 and diff.kept_total == 2
    assert [row.dropped for row in diff.rows] == [False, True, False, True]
    assert [row.output_ordinal for row in diff.rows] == [0, None, 1, None]


def _dedupe_stage() -> Stage:
    return parse_stage({
        "id": "keep", "description": "Keep", "type": "dedupe",
        "inputs": [{"id": LOAD_ID}],
        "dedupe": {"keys": ["name"], "keep": "highest", "by": "val"},
        "signature": {"form": "extends", "reads": reads_of(LOAD_ID, _IN_COLUMNS)},
    })


def test_a_dedupe_gets_the_same_dropped_rows_view(tmp_path: Path) -> None:
    """It drew no diff at all until the type was wired into FILTER_TYPES."""
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "b", "c"], "val": [1, 2, 3, 4]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame(
        {"name": ["a", "b", "c"], "val": [1, 3, 4]}))
    _write_lineage(tmp_path, "keep", kept=[0, 2, 3])

    diff = _diff(tmp_path, _dedupe_stage(), out_rel)

    assert diff is not None and diff.kind == FILTER_ROWS_KIND
    assert [row.dropped for row in diff.rows] == [False, True, False, False]
    assert diff.rows[1].cells == ["b", "2"]
    assert diff.dropped_total == 1 and diff.kept_total == 3


def test_at_rows_draws_the_named_rows_and_not_the_first(tmp_path: Path) -> None:
    # One figure's rows sit wherever the run put them, not at the head.
    total = PREVIEW_ROWS_SHOWN + 4
    _write_output(tmp_path, LOAD_ID, _numbered_frame(total))
    changed = _numbered_frame(total)
    changed["name"] = changed["name"].str.upper()
    out_rel = _write_output(tmp_path, "classify", changed)

    diff = build_stage_diff(place_stage(_row_stage(_IN_COLUMNS)), tmp_path, out_rel,
                            {LOAD_ID: _LOAD_PATH}, at_rows=[total - 1, 1])

    assert isinstance(diff, RowAlignedDiff)
    assert [row[0].text for row in diff.rows] == [
        changed["name"].iloc[total - 1], changed["name"].iloc[1]]
    # The counts stay the whole frame's — a window never restates the run.
    assert diff.rows_total == total and diff.changed_cells_total == total


def test_at_rows_drops_an_ordinal_the_frame_does_not_have(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, _numbered_frame(3))
    out_rel = _write_output(tmp_path, "classify", _numbered_frame(3))

    diff = build_stage_diff(place_stage(_row_stage(_IN_COLUMNS)), tmp_path, out_rel,
                            {LOAD_ID: _LOAD_PATH}, at_rows=[1, 99])

    assert isinstance(diff, RowAlignedDiff) and len(diff.rows) == 1


def test_a_drawn_row_carries_where_it_came_from(tmp_path: Path) -> None:
    # The lineage link beside a row is built from this.
    total = PREVIEW_ROWS_SHOWN + 4
    _write_output(tmp_path, LOAD_ID, _numbered_frame(total))
    out_rel = _write_output(tmp_path, "classify", _numbered_frame(total))
    stage = place_stage(_row_stage(_IN_COLUMNS))

    picked = build_stage_diff(stage, tmp_path, out_rel, {LOAD_ID: _LOAD_PATH},
                              at_rows=[total - 1, 1])
    head = build_stage_diff(stage, tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(picked, RowAlignedDiff) and isinstance(head, RowAlignedDiff)
    assert picked.row_ordinals == [total - 1, 1] and picked.opens_on_the_first is False
    assert head.row_ordinals == list(range(PREVIEW_ROWS_SHOWN))
    assert head.opens_on_the_first is True
