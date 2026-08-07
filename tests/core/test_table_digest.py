"""A table's digest is its VALUES: the same data digests alike however it was stored,
and differently as soon as a value, a column or a row order changes."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.core.frames import compute_table_digest

TYPES = {"amount": "int", "rate": "float", "name": "str", "when": "date", "flag": "bool"}


def _as_read_from_parquet() -> pd.DataFrame:
    return pd.DataFrame({
        "amount": [1, 2],
        "rate": [1.5, 2.0],
        "name": ["a", None],
        "when": pd.to_datetime(["2026-01-01", "2026-02-02"]),
        "flag": [True, False],
    })


def _as_read_from_a_spreadsheet() -> pd.DataFrame:
    """As an xlsx or csv read gives it back: every number a float, every blank NaN."""
    return pd.DataFrame({
        "amount": [1.0, 2.0],
        "rate": [1.5, 2],
        "name": ["a", np.nan],
        "when": pd.to_datetime(["2026-01-01", "2026-02-02"]),
        "flag": [True, False],
    })


def test_the_same_table_digests_alike_whatever_it_was_read_from() -> None:
    assert compute_table_digest(_as_read_from_parquet(), TYPES) == compute_table_digest(
        _as_read_from_a_spreadsheet(), TYPES
    )


def test_the_declared_types_are_what_make_that_true() -> None:
    """Without them the dtype drift still shows — pass the stage's own output schema."""
    assert compute_table_digest(_as_read_from_parquet()) != compute_table_digest(
        _as_read_from_a_spreadsheet()
    )


def test_a_changed_value_changes_the_digest() -> None:
    changed = _as_read_from_parquet()
    changed.loc[0, "amount"] = 3
    assert compute_table_digest(changed, TYPES) != compute_table_digest(
        _as_read_from_parquet(), TYPES
    )


def test_a_reordered_column_is_a_different_table() -> None:
    frame = _as_read_from_parquet()
    assert compute_table_digest(frame[list(reversed(frame.columns))], TYPES) != (
        compute_table_digest(frame, TYPES)
    )


def test_a_reordered_row_is_a_different_table() -> None:
    frame = _as_read_from_parquet()
    assert compute_table_digest(frame.iloc[::-1], TYPES) != compute_table_digest(
        frame, TYPES
    )


def test_every_null_form_reads_as_the_same_absence() -> None:
    types = {"name": "str"}
    nones = pd.DataFrame({"name": [None, "a"]})
    nans = pd.DataFrame({"name": [np.nan, "a"]})
    pd_nas = pd.DataFrame({"name": [pd.NA, "a"]})
    assert (
        compute_table_digest(nones, types)
        == compute_table_digest(nans, types)
        == compute_table_digest(pd_nas, types)
    )


def test_a_value_that_is_not_a_number_digests_rather_than_raising() -> None:
    """The validator reports that schema violation; the digest still has to produce one."""
    frame = pd.DataFrame({"amount": ["not a number", "2"]})
    assert compute_table_digest(frame, {"amount": "int"})


def test_an_index_is_not_part_of_the_identity() -> None:
    """It does not survive the parquet round trip a stage's output takes."""
    frame = _as_read_from_parquet()
    assert compute_table_digest(frame.set_axis([7, 9]), TYPES) == compute_table_digest(
        frame, TYPES
    )
