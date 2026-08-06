"""Frozen fingerprint values. These key 718,155 stage-cache entries as of
2026-08-06: a changed hash does not fail, it silently orphans every recorded row
and every human review decision under the old key. Changing a value here is a
migration, not a test update — see docs/pandas-seam.md and #437's failure #1.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.frames import compute_frame_fingerprint, compute_frames_fingerprint, list_rows
from app.core.stage_cache import compute_row_fingerprint

# One frame exercising every representation that has moved under us before: an
# int column that met a null (pandas upcasts to float), a list column (parquet
# returns it as ndarray unless read through the seam), a timestamp (pd.Timestamp
# vs datetime.datetime), a bool, and a plain string.
_FROZEN_FRAME = pd.DataFrame(
    {
        "id": ["a", "b"],
        "n": [1, None],
        "score": [0.5, 2.0],
        "tags": [["x", "y"], []],
        "when": pd.to_datetime(["2026-01-01", "2026-02-01"]),
        "flag": [True, False],
    }
)

_FROZEN_ROW_FINGERPRINTS = ["f7b3dbcee3434b6f", "8cf1a35ba171fc08"]
_FROZEN_FRAME_FINGERPRINT = "c3fcf60bf0750e1b"


def test_row_fingerprints_have_not_moved():
    assert [compute_row_fingerprint(r) for r in list_rows(_FROZEN_FRAME)] == (
        _FROZEN_ROW_FINGERPRINTS
    )


def test_frame_fingerprint_has_not_moved():
    assert compute_frame_fingerprint(_FROZEN_FRAME) == _FROZEN_FRAME_FINGERPRINT


@pytest.mark.parametrize(
    "column, values",
    [
        ("tz_aware", pd.to_datetime(["2026-01-01 12:00:00"]).tz_localize("UTC")),
        ("nanoseconds", pd.to_datetime(["2026-01-01 00:00:00.123456789"])),
        ("empty_list", [[]]),
        ("all_null", [None]),
        ("big_int", [2**62]),
        ("float_nan", [float("nan")]),
    ],
)
def test_a_round_trip_through_the_frame_seam_keeps_a_rows_identity(column, values, tmp_path):
    """The representations most likely to drift across storage — each keys the cache."""
    from app.core.frames import read_frame_file, write_frame_file

    frame = pd.DataFrame({column: values})
    before = [compute_row_fingerprint(row) for row in list_rows(frame)]
    write_frame_file(frame, tmp_path / "f.parquet")
    after = [compute_row_fingerprint(row) for row in list_rows(read_frame_file(tmp_path / "f.parquet"))]
    assert before == after


def test_frames_fingerprint_has_not_moved():
    assert compute_frames_fingerprint([_FROZEN_FRAME]) == "4689154503d07474"
