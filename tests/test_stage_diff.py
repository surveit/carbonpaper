"""The Outputs-pane stage diff (app.web.stage_diff): added-column and
changed-cell detection for 1:1 stages, the dropped-rows report for
filter_rows read off the lineage sidecar, None for every out-of-scope stage
type, and None (fallback) wherever the alignment cannot be verified."""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.models.stage import Stage, parse_stage
from app.runtime.lineage import (
    TRACE_SOURCE_ROW_KEY,
    TRACE_SOURCE_STAGE_KEY,
    lineage_sidecar_path,
)
from app.web.stage_diff import (
    DROPPED_ROWS_KIND,
    ROW_ALIGNED_KIND,
    RowAlignedDiff,
    build_stage_diff,
)

LOAD_ID = "load"
_LOAD_PATH = f"outputs/{LOAD_ID}.parquet"

_IN_COLUMNS = [{"name": "name", "type": "str"}, {"name": "val", "type": "int"}]
_OUT_COLUMNS = _IN_COLUMNS + [{"name": "label", "type": "str"}]


def _row_stage(output_columns: list[dict] | None = None) -> Stage:
    return parse_stage({
        "id": "classify", "name": "Classify", "type": "python_row_function",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _IN_COLUMNS}}],
        "function": {"kind": "inline",
                     "code": "def transform(row):\n    return row\n"},
        "output_schema": {"columns": output_columns or _OUT_COLUMNS},
    })


def _filter_stage() -> Stage:
    return parse_stage({
        "id": "keep", "name": "Keep", "type": "filter_rows",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _IN_COLUMNS}}],
        "filter": {"code": "def should_include(row):\n    return True\n"},
        "output_schema": {"columns": _IN_COLUMNS},
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
    }).to_parquet(lineage_sidecar_path(run_dir, stage_id), index=False)


def _diff(run_dir: Path, stage_def: Stage, out_rel: str):
    return build_stage_diff(stage_def, run_dir, out_rel, {LOAD_ID: _LOAD_PATH})


# ─── row-aligned: added columns, changed cells, unchanged passthrough ────────

def test_a_column_the_stage_added_is_named_and_marked(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a", "b"], "val": [1, 2]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a", "b"], "val": [1, 2], "label": ["x", "y"]}))

    diff = _diff(tmp_path, _row_stage(), out_rel)

    assert diff is not None and diff.kind == ROW_ALIGNED_KIND
    assert diff.added_column_names == ["label"]
    label_cells = [row[2] for row in diff.rows]
    assert all(cell.added for cell in label_cells)
    assert diff.changed_cells_total == 0


def test_changed_cells_are_counted_over_the_whole_frame_and_marked(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "c"], "val": [1, 2, 3]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame(
        {"name": ["a", "B", "C"], "val": [1, 2, 3]}))

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert diff is not None
    name_column = next(c for c in diff.columns if c.name == "name")
    assert name_column.changed_cells == 2
    assert diff.changed_cells_total == 2
    changed_cell = diff.rows[1][0]
    assert changed_cell.changed and changed_cell.was == "b" and changed_cell.text == "B"
    untouched_cell = diff.rows[0][0]
    assert not untouched_cell.changed and untouched_cell.was is None


def test_an_unchanged_passthrough_reports_every_value_carried(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "classify", frame.copy())

    diff = _diff(tmp_path, _row_stage(_IN_COLUMNS), out_rel)

    assert diff is not None
    assert diff.changed_cells_total == 0
    assert diff.added_column_names == [] and diff.removed_column_names == []
    assert all(not cell.changed and not cell.added for row in diff.rows for cell in row)


def test_a_column_the_stage_dropped_is_named(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "classify", pd.DataFrame({"name": ["a"]}))

    diff = _diff(tmp_path, _row_stage([{"name": "name", "type": "str"}]), out_rel)

    assert diff is not None
    assert diff.removed_column_names == ["val"]


def test_an_llm_transform_is_admitted_to_the_row_aligned_diff(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "judge", "name": "Judge", "type": "llm_transform",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _IN_COLUMNS,
                                              "primary_key": ["name"]}}],
        "llm": {"prompt_data_template": "{name}"},
        "output_schema": {"columns": _OUT_COLUMNS, "primary_key": ["name"]},
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "judge", pd.DataFrame(
        {"name": ["a"], "val": [1], "label": ["x"]}))

    diff = build_stage_diff(stage, tmp_path, out_rel, {LOAD_ID: _LOAD_PATH})

    assert isinstance(diff, RowAlignedDiff) and diff.kind == ROW_ALIGNED_KIND
    assert diff.added_column_names == ["label"]


# ─── filter_rows: the dropped-rows report ────────────────────────────────────

def test_filter_rows_reports_the_dropped_rows_by_input_ordinal(tmp_path: Path) -> None:
    _write_output(tmp_path, LOAD_ID, pd.DataFrame(
        {"name": ["a", "b", "c", "d"], "val": [1, 2, 3, 4]}))
    out_rel = _write_output(tmp_path, "keep", pd.DataFrame(
        {"name": ["a", "c"], "val": [1, 3]}))
    _write_lineage(tmp_path, "keep", kept=[0, 2])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None and diff.kind == DROPPED_ROWS_KIND
    assert diff.dropped_total == 2 and diff.kept_total == 2 and diff.input_total == 4
    assert [row.ordinal for row in diff.dropped] == [1, 3]
    assert diff.dropped[0].cells == ["b", "2"]


def test_filter_rows_that_dropped_nothing_still_gets_a_report(tmp_path: Path) -> None:
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "keep", frame.copy())
    _write_lineage(tmp_path, "keep", kept=[0, 1])

    diff = _diff(tmp_path, _filter_stage(), out_rel)

    assert diff is not None and diff.kind == DROPPED_ROWS_KIND
    assert diff.dropped_total == 0 and diff.dropped == []


# ─── out-of-scope stage types: no diff, ever ─────────────────────────────────

def test_a_frame_function_gets_no_diff_even_at_matching_row_counts(tmp_path: Path) -> None:
    # Same row count is not the contract — a frame function may reorder rows,
    # so a positional diff would be a fabricated alignment.
    stage = parse_stage({
        "id": "reshape", "name": "Reshape", "type": "python_frame_function",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _IN_COLUMNS}}],
        "function": {"kind": "inline",
                     "code": "def transform(df):\n    return df\n"},
        "output_schema": {"columns": _IN_COLUMNS},
    })
    frame = pd.DataFrame({"name": ["a", "b"], "val": [1, 2]})
    _write_output(tmp_path, LOAD_ID, frame)
    out_rel = _write_output(tmp_path, "reshape", frame.copy())

    assert build_stage_diff(stage, tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}) is None


def test_a_union_gets_no_diff(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "both", "name": "Both", "type": "union",
        "inputs": [{"id": LOAD_ID, "schema": {"columns": _IN_COLUMNS}},
                   {"id": "more", "schema": {"columns": _IN_COLUMNS}}],
        "union": {},
        "output_schema": {"columns": _IN_COLUMNS},
    })
    _write_output(tmp_path, LOAD_ID, pd.DataFrame({"name": ["a"], "val": [1]}))
    out_rel = _write_output(tmp_path, "both", pd.DataFrame({"name": ["a"], "val": [1]}))

    assert build_stage_diff(stage, tmp_path, out_rel, {LOAD_ID: _LOAD_PATH}) is None


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
