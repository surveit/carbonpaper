"""The frame payload accessors on the cache seam (app/core/stage_cache.py).

A whole-frame transform may index positionally or depend on order, so column
order AND row order are part of a frame's identity — unlike a row, whose column
order is deliberately irrelevant.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.core.errors import FrameNotSerializableError
from app.core.stage_cache import (
    ReadOnlyStageCache,
    StageCache,
    _compute_frame_fingerprint,
    _compute_frames_fingerprint,
)

PROJECT = "frame-seam-tests"
STAGE_KEY = (PROJECT, "stage", "deffp")
INPUTS = [pd.DataFrame({"in": [1]})]


def test_the_same_frame_fingerprints_the_same():
    left = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    right = pd.DataFrame({"x": [1, 2], "y": ["a", "b"]})
    assert _compute_frame_fingerprint(left) == _compute_frame_fingerprint(right)


def test_a_changed_cell_changes_the_fingerprint():
    before = pd.DataFrame({"x": [1, 2]})
    after = pd.DataFrame({"x": [1, 3]})
    assert _compute_frame_fingerprint(before) != _compute_frame_fingerprint(after)


def test_reordering_the_rows_changes_the_fingerprint():
    frame = pd.DataFrame({"x": [1, 2]})
    reordered = frame.iloc[::-1].reset_index(drop=True)
    assert _compute_frame_fingerprint(frame) != _compute_frame_fingerprint(reordered)


def test_reordering_the_columns_changes_the_fingerprint():
    frame = pd.DataFrame({"x": [1], "y": [2]})
    assert _compute_frame_fingerprint(frame) != _compute_frame_fingerprint(frame[["y", "x"]])


def test_renaming_a_column_changes_the_fingerprint():
    assert (
        _compute_frame_fingerprint(pd.DataFrame({"x": [1]}))
        != _compute_frame_fingerprint(pd.DataFrame({"z": [1]}))
    )


def test_every_null_form_collapses_to_one_identity():
    """A parquet round trip may hand back pd.NA where None went in; that must
    not change what the frame is."""
    with_none = pd.DataFrame({"x": [None]}, dtype=object)
    with_na = pd.DataFrame({"x": [pd.NA]}, dtype=object)
    with_nan = pd.DataFrame({"x": [float("nan")]}, dtype=object)
    assert (
        _compute_frame_fingerprint(with_none)
        == _compute_frame_fingerprint(with_na)
        == _compute_frame_fingerprint(with_nan)
    )


def test_the_row_index_is_not_part_of_the_identity():
    """Only the cells, their column order and their row order count — a
    non-default index survives a parquet round trip as nothing at all."""
    frame = pd.DataFrame({"x": [1, 2]})
    reindexed = frame.copy()
    reindexed.index = pd.Index([7, 9])
    assert _compute_frame_fingerprint(frame) == _compute_frame_fingerprint(reindexed)


def test_several_frames_fingerprint_in_the_order_given():
    left = pd.DataFrame({"x": [1]})
    right = pd.DataFrame({"y": [2]})
    assert _compute_frames_fingerprint([left, right]) != _compute_frames_fingerprint([right, left])


def test_one_frame_alone_still_fingerprints_as_a_sequence():
    frame = pd.DataFrame({"x": [1]})
    assert _compute_frames_fingerprint([frame]) == _compute_frames_fingerprint([frame.copy()])


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
    """A caller hands the ordered input frames and never a fingerprint, so a
    changed input resolves to a different entry without the caller doing
    anything."""
    _record(pd.DataFrame({"x": [1]}))
    changed = [pd.DataFrame({"in": [2]})]
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, changed) is None


def test_the_accessors_key_on_the_input_order():
    left, right = pd.DataFrame({"x": [1]}), pd.DataFrame({"y": [2]})
    _record(pd.DataFrame({"out": [1]}), inputs=[left, right])
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, [right, left]) is None
    assert ReadOnlyStageCache().find_cached_frame(*STAGE_KEY, [left, right]) is not None


def test_the_read_only_view_cannot_record_a_frame():
    """Structurally absent, exactly as `record` is — not withheld by a check."""
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
