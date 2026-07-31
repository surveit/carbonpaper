import numpy as np
import pandas as pd
import pytest

from app.core.frames import (
    FrameStore,
    collapse_null_forms,
    is_null_form,
    is_sequence_cell,
    list_rows,
)


@pytest.fixture
def frames(tmp_path):
    return FrameStore(tmp_path)


def test_save_then_load_roundtrips(frames):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    frames.save_frame("run_output", "proj/run1/stageA", df)
    loaded = frames.load_frame("run_output", "proj/run1/stageA")
    pd.testing.assert_frame_equal(loaded, df)


def test_load_missing_returns_none(frames):
    assert frames.load_frame("run_output", "proj/absent") is None


def test_write_once_refuses_overwrite(frames):
    df = pd.DataFrame({"a": [1]})
    frames.save_frame("eval_data", "proj/set1", df, overwrite=False)
    with pytest.raises(FileExistsError):
        frames.save_frame("eval_data", "proj/set1", df, overwrite=False)


@pytest.mark.parametrize("bad_id", ["../escape", "C:/escape", "a\\b"])
def test_unsafe_id_rejected(frames, bad_id):
    with pytest.raises(ValueError):
        frames.save_frame("run_output", bad_id, pd.DataFrame({"a": [1]}))
    # validate_id must run before any path is built or written, so the location
    # that path construction would have produced never appears on disk — including
    # when a drive-absolute id would otherwise land outside frames.root entirely.
    escaped_path = frames.root / "run_output" / f"{bad_id}.parquet"
    assert not escaped_path.exists()


@pytest.mark.parametrize("bad_collection", ["../evil", "C:/escape", "a\\b"])
def test_unsafe_collection_rejected(frames, bad_collection):
    with pytest.raises(ValueError):
        frames.save_frame(bad_collection, "proj/1", pd.DataFrame({"a": [1]}))
    # Same escape risk as an unsafe id (validate_id must run before any path is
    # built), just on the other path segment — so it never lands on disk either.
    escaped_path = frames.root / bad_collection / "proj/1.parquet"
    assert not escaped_path.exists()


def test_list_rows_gives_one_str_keyed_dict_per_row():
    """The str pinning is the point: a frame whose column labels are integers
    still yields keys a caller can look up by name."""
    frame = pd.DataFrame({"a": [1, 2], 3: ["x", "y"]})
    assert list_rows(frame) == [{"a": 1, "3": "x"}, {"a": 2, "3": "y"}]


def test_list_rows_of_an_empty_frame_is_empty():
    assert list_rows(pd.DataFrame({"a": []})) == []


@pytest.mark.parametrize("value", [None, float("nan"), pd.NA, pd.NaT])
def test_is_null_form_accepts_every_pandas_null_form(value):
    assert is_null_form(value)


@pytest.mark.parametrize("value", [0, "", False, [], "nan", np.int64(0)])
def test_is_null_form_rejects_non_nulls_including_falsy_ones(value):
    assert not is_null_form(value)


@pytest.mark.parametrize("cell", [[1, 2], (1, 2), np.array([1, 2]), {"a": 1}, {1, 2}])
def test_is_null_form_survives_an_array_valued_cell(cell):
    """The reason this is expressed as collapse_null_forms rather than pd.isna:
    pd.isna on a list/array cell returns an elementwise array whose truth value
    is ambiguous. Each of these must simply answer False."""
    assert not is_null_form(cell)


def test_is_null_form_agrees_with_collapse_null_forms():
    for value in [None, float("nan"), pd.NA, pd.NaT, 0, "x", [1], np.nan]:
        assert is_null_form(value) == (collapse_null_forms(value) is None)


@pytest.mark.parametrize("cell", [[1], (1,), np.array([1])])
def test_is_sequence_cell_accepts_lists_tuples_and_arrays(cell):
    # ndarray matters: a list column round-trips through parquet as one.
    assert is_sequence_cell(cell)


@pytest.mark.parametrize("cell", ["ab", {"a": 1}, 1, None])
def test_is_sequence_cell_rejects_scalars_strings_and_dicts(cell):
    assert not is_sequence_cell(cell)
