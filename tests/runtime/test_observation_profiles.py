"""app.runtime.observation: per-column observed value profiles of an input frame —
full distinct set under the cap, count + sample over it, null/row counts always —
and profile_input_stage loading a stage's bound file through read_input_data
(so declared types hold) while failing loudly on every miss."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models.observation import DISTINCT_FULL_SET_CAP, OVER_CAP_SAMPLE_SIZE
from app.models.stage import parse_stage
from app.runtime.observation import profile_frame, profile_input_stage


# ── profile_frame ────────────────────────────────────────────────────────────

def test_small_column_reports_its_complete_sorted_set() -> None:
    profile = profile_frame(pd.DataFrame({"status": ["granted", "filed", "granted"]}))
    [column] = profile.columns
    assert column.name == "status"
    assert column.values == ["filed", "granted"]
    assert column.sample is None
    assert column.distinct_count == 2
    assert column.null_count == 0
    assert column.row_count == 3
    assert profile.row_count == 3


def test_nulls_are_counted_and_never_appear_as_values() -> None:
    profile = profile_frame(pd.DataFrame({"city": ["Boston", None, "Boston", None]}))
    [column] = profile.columns
    assert column.values == ["Boston"]
    assert column.null_count == 2
    assert column.distinct_count == 1
    assert column.row_count == 4


def test_over_cap_column_reports_count_and_sample_not_the_set() -> None:
    n = DISTINCT_FULL_SET_CAP + 1
    profile = profile_frame(pd.DataFrame({"id": [f"id_{i:04d}" for i in range(n)]}))
    [column] = profile.columns
    assert column.values is None
    assert column.distinct_count == n
    assert column.sample == sorted(f"id_{i:04d}" for i in range(n))[:OVER_CAP_SAMPLE_SIZE]


def test_at_cap_column_still_reports_the_complete_set() -> None:
    n = DISTINCT_FULL_SET_CAP
    profile = profile_frame(pd.DataFrame({"code": [f"c{i:03d}" for i in range(n)]}))
    [column] = profile.columns
    assert column.values is not None and len(column.values) == n
    assert column.sample is None


def test_non_string_cells_report_their_str_form() -> None:
    profile = profile_frame(pd.DataFrame({"n": [2, 1, 2]}))
    [column] = profile.columns
    assert column.values == ["1", "2"]


def test_empty_frame_profiles_as_zero_rows_with_empty_sets() -> None:
    profile = profile_frame(pd.DataFrame({"a": pd.Series([], dtype=object)}))
    assert profile.row_count == 0
    [column] = profile.columns
    assert column.values == []
    assert column.distinct_count == 0


# ── profile_input_stage ──────────────────────────────────────────────────────

def _input_stage(path: Path | None, columns: list[dict]):
    params: dict = {} if path is None else {"path": str(path)}
    return parse_stage({
        "id": "load", "name": "load", "type": "input_data",
        "connector": {"kind": "file", "params": params},
        "output_schema": {"columns": columns},
    })


def test_profiles_the_bound_file_honouring_declared_types(tmp_path: Path) -> None:
    path = tmp_path / "in.csv"
    path.write_text("zip,status\n02134,filed\n90210,granted\n", encoding="utf-8")
    stage = _input_stage(path, [{"name": "zip", "type": "str"},
                                {"name": "status", "type": "str"}])
    profile = profile_input_stage(stage)
    zip_col = profile.column_named("zip")
    assert zip_col is not None
    # Read through read_input_data, so the declared str pins the zero-padded zip.
    assert zip_col.values == ["02134", "90210"]


def test_unbound_path_raises_rather_than_profiling_nothing(tmp_path: Path) -> None:
    stage = _input_stage(None, [{"name": "a", "type": "str"}])
    with pytest.raises(ValueError, match="no file bound"):
        profile_input_stage(stage)


def test_missing_file_raises(tmp_path: Path) -> None:
    stage = _input_stage(tmp_path / "gone.csv", [{"name": "a", "type": "str"}])
    with pytest.raises(FileNotFoundError):
        profile_input_stage(stage)


def test_non_input_stage_is_refused(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "agg", "name": "agg", "type": "aggregate",
        "aggregate": {"group_by": ["a"],
                      "aggregations": [{"output_column": "n", "formula": "count"}]},
        "inputs": [{"id": "load", "schema": {"columns": [{"name": "a", "type": "str"}]}}],
        "output_schema": {"columns": [{"name": "a", "type": "str"},
                                      {"name": "n", "type": "int"}]},
    })
    with pytest.raises(ValueError, match="not `input_data`"):
        profile_input_stage(stage)
