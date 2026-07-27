"""Tests for Column.coerce_text (app/models/schema.py): one form field's raw
string landed as a value the column's whole declaration allows — type,
nullability, enum vocabulary and numeric range."""
from __future__ import annotations

import datetime

import pytest

from app.models import Column


@pytest.mark.parametrize(("column_type", "text", "expected"), [
    ("str", "hello", "hello"),
    ("int", "42", 42),
    ("int", "-7", -7),
    ("float", "1.5", 1.5),
    ("bool", "true", True),
    ("bool", "False", False),
    ("bool", "1", True),
    ("bool", "no", False),
    ("date", "2026-07-27", datetime.date(2026, 7, 27)),
    ("datetime", "2026-07-27T10:30:00", datetime.datetime(2026, 7, 27, 10, 30)),
])
def test_each_scalar_type_round_trips_from_text(column_type, text, expected):
    assert Column(name="c", type=column_type).coerce_text(text) == expected


def test_surrounding_whitespace_is_not_data():
    assert Column(name="c", type="int").coerce_text("  42  ") == 42


def test_blank_text_on_a_nullable_column_is_none():
    assert Column(name="c", type="int", nullable=True).coerce_text("   ") is None


def test_blank_text_on_a_non_nullable_column_raises():
    with pytest.raises(ValueError, match="not nullable"):
        Column(name="c", type="int", nullable=False).coerce_text("")


@pytest.mark.parametrize(("column_type", "text"), [
    ("int", "1.5"),
    ("int", "twelve"),
    ("float", "lots"),
    ("bool", "maybe"),
    ("date", "27/07/2026"),
    ("datetime", "yesterday"),
])
def test_unparseable_text_raises_naming_the_column_and_the_text(column_type, text):
    with pytest.raises(ValueError, match="'c'") as exc:
        Column(name="c", type=column_type).coerce_text(text)
    assert text in str(exc.value)


@pytest.mark.parametrize("column_type", ["json", "list[str]", "list[json]"])
def test_a_non_scalar_column_cannot_be_entered_as_text(column_type):
    column = (
        Column(name="c", type=column_type, value_type="str")
        if "json" in column_type else Column(name="c", type=column_type)
    )
    with pytest.raises(ValueError, match="not a scalar"):
        column.coerce_text("anything")


def test_a_value_outside_the_declared_enum_is_refused():
    column = Column(name="verdict", type="str", enum=["yes", "no"])
    assert column.coerce_text("yes") == "yes"
    with pytest.raises(ValueError, match="not one of the declared values") as exc:
        column.coerce_text("maybe")
    assert "verdict" in str(exc.value) and "yes" in str(exc.value)


@pytest.mark.parametrize(("text", "message"), [("-3", "below"), ("3", "above")])
def test_a_value_outside_the_declared_range_is_refused(text, message):
    column = Column(name="score", type="int", range=[-2, 2])
    assert column.coerce_text("2") == 2
    with pytest.raises(ValueError, match=message) as exc:
        column.coerce_text(text)
    assert "score" in str(exc.value)


def test_an_unbounded_range_side_constrains_only_the_other_side():
    column = Column(name="score", type="float", range=["-inf", 10])
    assert column.coerce_text("-99999") == -99999.0
    with pytest.raises(ValueError, match="above"):
        column.coerce_text("10.5")
