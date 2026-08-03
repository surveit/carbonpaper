"""The cross-row rules shared by the runner and the stage-test suite validator."""
from __future__ import annotations

import pandas as pd

from app.core.frame_checks import (
    find_duplicate_row_violations,
    find_frame_violations,
    find_primary_key_violations,
)


def test_a_clean_frame_has_no_violations():
    df = pd.DataFrame({"id": ["a", "b"], "n": [1, 2]})
    assert find_frame_violations(df, primary_key=["id"]) == []


def test_an_undeclared_key_is_nothing_to_check():
    df = pd.DataFrame({"id": ["a", "a"]})
    assert find_primary_key_violations(df, None) == []
    assert find_primary_key_violations(df, []) == []


def test_a_key_column_the_frame_lacks_is_left_to_the_per_column_check():
    # Reporting it here too would say "Missing column" in second, vaguer words.
    df = pd.DataFrame({"n": [1, 2]})
    assert find_primary_key_violations(df, ["id"]) == []


def test_a_repeated_key_names_its_columns():
    df = pd.DataFrame({"id": ["a", "a", "b"], "n": [1, 2, 3]})
    violation = find_primary_key_violations(df, ["id"])[0]
    assert violation.columns == ["id"]
    assert violation.message == "Primary key duplicated on 1 row(s)"


def test_rows_differing_anywhere_are_not_duplicates():
    # The declared key plays no part in row identity: same key, distinct rows.
    df = pd.DataFrame({"id": ["a", "a"], "n": [1, 2]})
    assert find_duplicate_row_violations(df) == []


def test_a_repeated_row_names_its_0_based_positions():
    df = pd.DataFrame({"id": ["a", "b", "a"], "n": [1, 2, 1]})
    violation = find_duplicate_row_violations(df)[0]
    assert violation.columns is None
    assert "rows [0, 2]" in violation.message
    assert "row_id" in violation.message  # points at the explicit-draws fix


def test_cells_of_different_types_sharing_a_face_value_stay_distinct():
    df = pd.DataFrame({"n": [1, "1"]})
    assert find_duplicate_row_violations(df) == []


def test_an_empty_frame_has_no_duplicates():
    assert find_duplicate_row_violations(pd.DataFrame()) == []


def test_groups_past_the_named_limit_are_counted_off_rather_than_listed():
    # Six duplicated pairs: five named, the sixth summarised.
    df = pd.DataFrame({"id": [letter for letter in "aabbccddeeff"]})
    message = find_duplicate_row_violations(df)[0].message
    assert message.count("rows [") == 5
    assert "(+1 more group(s))" in message


def test_both_rules_report_together():
    df = pd.DataFrame({"id": ["a", "a"], "n": [1, 1]})
    messages = [v.message for v in find_frame_violations(df, primary_key=["id"])]
    assert len(messages) == 2
    assert any("Primary key duplicated" in m for m in messages)
    assert any("exact duplicate rows" in m for m in messages)
