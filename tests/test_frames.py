import numpy as np
import pandas as pd
import pytest

from app.core.frames import (
    FrameStore,
    collapse_null_forms,
    is_bool_cell,
    is_exact_float_cell,
    is_exact_int_cell,
    is_missing_cell,
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
    """pd.isna on an array cell returns an elementwise array; each of these must answer
    False."""
    assert not is_null_form(cell)


def test_is_null_form_agrees_with_collapse_null_forms():
    for value in [None, float("nan"), pd.NA, pd.NaT, 0, "x", [1], np.nan]:
        assert is_null_form(value) == (collapse_null_forms(value) is None)


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), pd.NA, pd.NaT, np.float32("nan"), np.datetime64("NaT", "ns")],
)
def test_is_missing_cell_accepts_every_form_including_the_two_is_null_form_misses(value):
    """Includes the two forms `is_null_form` deliberately does not catch."""
    assert is_missing_cell(value)


@pytest.mark.parametrize("cell", [[1, 2], (1, 2), np.array([1, 2]), {"a": 1}])
def test_is_missing_cell_rejects_a_sequence_or_mapping_cell(cell):
    assert not is_missing_cell(cell)


@pytest.mark.parametrize("value", [0, "", False, [], "nan", np.int64(0), 2.0])
def test_is_missing_cell_rejects_non_nulls_including_falsy_ones(value):
    assert not is_missing_cell(value)


# is_exact_int_cell/is_exact_float_cell deliberately differ from _is_int_cell/
# _is_float_cell (lossy-tolerant, for column-type checks: a whole-valued float
# passes as int there) — do not merge these pairs.
@pytest.mark.parametrize("value", [2, np.int64(2), -5])
def test_is_exact_int_cell_accepts_real_ints(value):
    assert is_exact_int_cell(value)


@pytest.mark.parametrize("value", [2.0, np.float64(2.0), True, "2", None, [2]])
def test_is_exact_int_cell_rejects_whole_floats_bools_and_non_ints(value):
    assert not is_exact_int_cell(value)


@pytest.mark.parametrize("value", [2.0, np.float64(2.0), 0.5])
def test_is_exact_float_cell_accepts_real_floats(value):
    assert is_exact_float_cell(value)


@pytest.mark.parametrize("value", [2, np.int64(2), True, "x", None])
def test_is_exact_float_cell_rejects_ints_bools_and_non_floats(value):
    assert not is_exact_float_cell(value)


@pytest.mark.parametrize("value", [True, False, np.bool_(True)])
def test_is_bool_cell_accepts_python_and_numpy_bools(value):
    assert is_bool_cell(value)


@pytest.mark.parametrize("value", [1, 0, 1.0, "true", None])
def test_is_bool_cell_rejects_non_bools(value):
    assert not is_bool_cell(value)


@pytest.mark.parametrize("cell", [[1], (1,), np.array([1])])
def test_is_sequence_cell_accepts_lists_tuples_and_arrays(cell):
    # ndarray matters: a list column round-trips through parquet as one.
    assert is_sequence_cell(cell)


@pytest.mark.parametrize("cell", ["ab", {"a": 1}, 1, None])
def test_is_sequence_cell_rejects_scalars_strings_and_dicts(cell):
    assert not is_sequence_cell(cell)
