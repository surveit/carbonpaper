"""app.services.observation.profile_frame: per-column observed value profiles —
values up to the caller's maximum, the TRUE distinct_count and null/row counts
always."""
from __future__ import annotations

import pandas as pd
import pytest

from app.models.observation import DEFAULT_MAX_DISTINCT_VALUES
from app.services.observation import profile_frame


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
