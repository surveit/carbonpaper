"""Columns that really turned up in a Meltwater export."""
from __future__ import annotations

import pytest

from app.core.file_shape import ColumnKind, measure_column_shape

# A Meltwater export's Country: geolocated for some rows, blank for the rest.
COUNTRY = ["", "Netherlands", "", "Poland", "Netherlands", "", "", "France", ""]


def shape(values, *, null_count=0, max_values=4):
    return measure_column_shape("col", values, null_count=null_count, max_values=max_values)


def test_an_empty_string_is_not_filled():
    country = shape(COUNTRY)
    assert (country.filled_count, country.blank_count, country.null_count) == (4, 5, 0)


def test_the_distinct_count_ignores_the_empty_strings():
    assert shape(COUNTRY).distinct_count == 3


def test_the_top_values_are_the_values_commonest_first():
    assert [(v.value, v.count) for v in shape(COUNTRY).top] == [
        ("Netherlands", 2), ("France", 1), ("Poland", 1)]


def test_a_column_holding_nothing_but_empty_strings_is_empty():
    assert shape(["", "", ""]).kind == ColumnKind.EMPTY


def test_a_column_of_nulls_is_empty_too():
    assert shape([], null_count=13865).kind == ColumnKind.EMPTY


def test_one_value_for_every_row_is_constant():
    assert shape(["Facebook", "Facebook", "Facebook"]).kind == ColumnKind.CONSTANT


def test_a_value_missing_from_some_rows_is_not_constant():
    assert shape(["Facebook", "Facebook", ""]).kind != ColumnKind.CONSTANT


def test_numbers_carry_their_range_and_a_histogram():
    reactions = shape(["0", "0", "1", "4", "120"])
    assert reactions.kind == ColumnKind.NUMBER
    assert (reactions.numbers.min, reactions.numbers.max) == (0.0, 120.0)
    assert reactions.numbers.median == 1.0
    assert sum(b.count for b in reactions.histogram) == 5


def test_the_median_is_weighted_by_how_many_rows_hold_each_value():
    assert shape(["0", "0", "0", "0", "9"]).numbers.median == 0.0


def test_dates_carry_a_timeline_oldest_first():
    dates = shape(["2026-07-22", "2026-07-20", "2026-07-22"])
    assert dates.kind == ColumnKind.DATE
    assert [(d.value, d.count) for d in dates.timeline] == [
        ("2026-07-20", 1), ("2026-07-22", 2)]


def test_prose_is_text_and_carries_its_lengths():
    # The index is what makes these distinct enough to read as prose.
    comments = [f"bonjour madame tout va bien {i}" for i in range(30)]
    opening = shape(["Honteux", *comments])
    assert opening.kind == ColumnKind.TEXT
    assert (opening.lengths.min, opening.lengths.max) == (7, 30)


def test_a_short_set_of_repeated_values_is_a_category():
    assert shape(["neutral", "negative", "positive", "neutral"]).kind == ColumnKind.CATEGORY


def test_it_refuses_to_show_no_values_at_all():
    with pytest.raises(ValueError, match="max_values"):
        measure_column_shape("col", COUNTRY, null_count=0, max_values=0)


def test_a_declared_str_of_digits_is_not_measured_as_a_number() -> None:
    # Texas district ids, as the STAAR export writes them: zero-padded and text.
    ids = ["001902", "001903", "001904", "001906"]
    sniffed = measure_column_shape("district_id", ids, null_count=0, max_values=8)
    assert sniffed.kind is ColumnKind.NUMBER
    assert sniffed.numbers is not None and sniffed.numbers.min == 1902.0
    declared = measure_column_shape(
        "district_id", ids, null_count=0, max_values=8, may_read_as_number=False)
    assert declared.kind is not ColumnKind.NUMBER
    assert declared.numbers is None
    assert declared.histogram == []
    # The values themselves are what a reader gets instead of a range.
    assert [seen.value for seen in declared.top] == ids


def test_the_declared_type_only_takes_the_number_reading_away() -> None:
    days = ["2026-01-02", "2026-01-02", "2026-01-03"]
    shape = measure_column_shape(
        "filed_on", days, null_count=0, max_values=8, may_read_as_number=False)
    assert shape.kind is ColumnKind.DATE
    assert [seen.value for seen in shape.timeline] == ["2026-01-02", "2026-01-03"]
