"""app.runtime.observation: per-column observed value profiles of an input frame —
values up to the caller's maximum, the TRUE distinct_count and null/row counts
always — and profile_input_stage loading a stage's bound file through
read_input_data (so declared types hold) while failing loudly on every miss."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES
from app.models.stage import parse_stage
from app.runtime.observation import profile_frame, profile_input_stage


# ── profile_frame ────────────────────────────────────────────────────────────

def test_small_column_reports_its_complete_sorted_set() -> None:
    profile = profile_frame(pd.DataFrame({"status": ["granted", "filed", "granted"]}))
    [column] = profile.columns
    assert column.name == "status"
    assert column.values == ["filed", "granted"]
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


def test_truncated_values_still_report_the_true_distinct_count() -> None:
    # The gap between distinct_count and len(values) is the ONLY thing standing
    # between a reader and mistaking a prefix for the whole vocabulary.
    n = DEFAULT_MAX_DISTINCT_VALUES + 1
    profile = profile_frame(pd.DataFrame({"id": [f"id_{i:04d}" for i in range(n)]}))
    [column] = profile.columns
    assert column.distinct_count == n
    assert len(column.values) == DEFAULT_MAX_DISTINCT_VALUES
    assert column.distinct_count > len(column.values)
    assert column.values == sorted(f"id_{i:04d}" for i in range(n))[:-1]


def test_default_maximum_is_applied_when_the_caller_names_none() -> None:
    n = DEFAULT_MAX_DISTINCT_VALUES
    profile = profile_frame(pd.DataFrame({"code": [f"c{i:03d}" for i in range(n)]}))
    [column] = profile.columns
    assert len(column.values) == n == column.distinct_count


def test_a_larger_caller_maximum_returns_the_whole_large_vocabulary() -> None:
    # A closed vocabulary can be far bigger than the default (commodity codes).
    n = DEFAULT_MAX_DISTINCT_VALUES * 10
    frame = pd.DataFrame({"code": [f"c{i:04d}" for i in range(n)]})
    [column] = profile_frame(frame, max_values=n).columns
    assert column.distinct_count == n == len(column.values)


def test_a_maximum_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="max_values must be at least 1"):
        profile_frame(pd.DataFrame({"a": ["x"]}), max_values=0)


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
    stage = _input_stage(path, [{"name": "zip", "type": "str", "nullable": True},
                                {"name": "status", "type": "str", "nullable": True}])
    profile = profile_input_stage(stage)
    zip_col = profile.column_named("zip")
    assert zip_col is not None
    # Read through read_input_data, so the declared str pins the zero-padded zip.
    assert zip_col.values == ["02134", "90210"]


def test_stage_profiling_honours_the_caller_maximum(tmp_path: Path) -> None:
    path = tmp_path / "in.csv"
    codes = [f"c{i:04d}" for i in range(DEFAULT_MAX_DISTINCT_VALUES + 5)]
    path.write_text("code\n" + "\n".join(codes) + "\n", encoding="utf-8")
    stage = _input_stage(path, [{"name": "code", "type": "str", "nullable": True}])

    default_column = profile_input_stage(stage).column_named("code")
    assert default_column is not None
    assert len(default_column.values) == DEFAULT_MAX_DISTINCT_VALUES
    assert default_column.distinct_count == len(codes)

    all_column = profile_input_stage(stage, max_values=len(codes)).column_named("code")
    assert all_column is not None
    assert all_column.values == sorted(codes)


def test_unbound_path_raises_rather_than_profiling_nothing(tmp_path: Path) -> None:
    stage = _input_stage(None, [{"name": "a", "type": "str", "nullable": True}])
    with pytest.raises(ValueError, match="no file bound"):
        profile_input_stage(stage)


def test_missing_file_raises(tmp_path: Path) -> None:
    stage = _input_stage(tmp_path / "gone.csv", [{"name": "a", "type": "str", "nullable": True}])
    with pytest.raises(FileNotFoundError):
        profile_input_stage(stage)


def test_non_input_stage_is_refused(tmp_path: Path) -> None:
    stage = parse_stage({
        "id": "agg", "name": "agg", "type": "aggregate",
        "aggregate": {"group_by": ["a"],
                      "aggregations": [{"output_column": "n", "formula": "count"}]},
        "inputs": [{"id": "load", "schema": {"columns": [{"name": "a", "type": "str", "nullable": True}]}}],
        "output_schema": {"columns": [{"name": "a", "type": "str", "nullable": True},
                                      {"name": "n", "type": "int", "nullable": True}]},
    })
    with pytest.raises(ValueError, match="not `input_data`"):
        profile_input_stage(stage)
