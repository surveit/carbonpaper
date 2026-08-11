"""A whole-frame transform may index positionally, so column order AND row order
are part of a frame's identity — unlike a row, whose column order is irrelevant.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import FrameNotSerializableError
from app.core.frames import compute_frame_fingerprint, compute_frames_fingerprint
from app.core.stage_cache import ReadOnlyStageCache, StageCache

PROJECT = "frame-seam-tests"
STAGE_KEY = (PROJECT, "stage", "deffp")
INPUTS = [pd.DataFrame({"in": [1]})]


def test_the_same_frame_fingerprints_the_same():
    left = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    right = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    assert compute_frame_fingerprint(left) == compute_frame_fingerprint(right)


def test_a_changed_cell_changes_the_fingerprint():
    before = pd.DataFrame({"x": [1, 2]})
    after = pd.DataFrame({"x": [1, 3]})
    assert compute_frame_fingerprint(before) != compute_frame_fingerprint(after)


def test_reordering_the_rows_changes_the_fingerprint():
    frame = pd.DataFrame({"x": [1, 2]})
    reordered = frame.iloc[::-1].reset_index(drop=True)
    assert compute_frame_fingerprint(frame) != compute_frame_fingerprint(reordered)


def test_reordering_the_columns_changes_the_fingerprint():
    frame = pd.DataFrame({"x": [1], "y": [2]})
    assert compute_frame_fingerprint(frame) != compute_frame_fingerprint(frame[["y", "x"]])


def test_renaming_a_column_changes_the_fingerprint():
    assert (
        compute_frame_fingerprint(pd.DataFrame({"x": [1]}))
        != compute_frame_fingerprint(pd.DataFrame({"z": [1]}))
    )


def test_every_null_form_a_parquet_round_trip_can_return_collapses_to_one_identity():
    with_none = pd.DataFrame({"x": [None]}, dtype=object)
    with_na = pd.DataFrame({"x": [pd.NA]}, dtype=object)
    with_nan = pd.DataFrame({"x": [float("nan")]}, dtype=object)
    assert (
        compute_frame_fingerprint(with_none)
        == compute_frame_fingerprint(with_na)
        == compute_frame_fingerprint(with_nan)
    )


def test_the_row_index_is_dropped_by_a_parquet_round_trip_so_it_is_not_the_identity():
    frame = pd.DataFrame({"x": [1, 2]})
    reindexed = frame.copy()
    reindexed.index = pd.Index([7, 9])
    assert compute_frame_fingerprint(frame) == compute_frame_fingerprint(reindexed)


def test_several_frames_fingerprint_in_the_order_given():
    left = pd.DataFrame({"x": [1]})
    right = pd.DataFrame({"y": [2]})
    assert compute_frames_fingerprint([left, right]) != compute_frames_fingerprint([right, left])


def test_one_frame_alone_still_fingerprints_as_a_sequence():
    frame = pd.DataFrame({"x": [1]})
    assert compute_frames_fingerprint([frame]) == compute_frames_fingerprint([frame.copy()])


# ── the payload channel ──────────────────────────────────────────────────────


def _record(frame: pd.DataFrame, inputs: list[pd.DataFrame] | None = None) -> None:
    StageCache().record_frame(
        project=STAGE_KEY[0], stage_id=STAGE_KEY[1], stage_fingerprint=STAGE_KEY[2],
        input_frames=INPUTS if inputs is None else inputs, frame=frame,
    )


def test_a_recorded_frame_reads_back():
    frame = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    _record(frame)
    read_back = ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, INPUTS)
    assert read_back is not None
    pd.testing.assert_frame_equal(read_back, frame)


def test_an_unrecorded_input_reads_back_as_none():
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, INPUTS) is None


def test_the_accessors_key_on_the_input_frames_themselves():
    _record(pd.DataFrame({"x": [1]}))
    changed = [pd.DataFrame({"in": [2]})]
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, changed) is None


def test_the_accessors_key_on_the_input_order():
    left, right = pd.DataFrame({"x": [1]}), pd.DataFrame({"y": [2]})
    _record(pd.DataFrame({"out": [1]}), inputs=[left, right])
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, [right, left]) is None
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, [left, right]) is not None


def test_the_read_only_view_has_no_record_attribute_rather_than_a_refusing_one():
    assert not hasattr(ReadOnlyStageCache(), "record_frame")


def test_a_frame_parquet_cannot_serialize_raises_a_named_error():
    unserializable = pd.DataFrame({"x": [{"nested": np.array([1, 2])}, 3]})
    with pytest.raises(FrameNotSerializableError):
        _record(unserializable)


def test_a_failed_record_leaves_no_half_written_entry():
    unserializable = pd.DataFrame({"x": [{"nested": np.array([1, 2])}, 3]})
    with pytest.raises(FrameNotSerializableError):
        _record(unserializable)
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, INPUTS) is None
