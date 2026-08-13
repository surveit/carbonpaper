import numpy as np
import pandas as pd
import pyarrow as pa
import pytest

from app.core.frames import (
    FrameStore,
    collapse_null_forms,
    compute_table_fingerprint,
    frame_to_table,
    is_bool_cell,
    is_exact_float_cell,
    is_exact_int_cell,
    is_missing_cell,
    is_null_form,
    is_sequence_cell,
    list_rows,
    list_table_rows,
    read_frame_column_names,
    read_frame_file,
    render_frame_as_csv_text,
    write_frame_file,
    write_frame_file_with_csv_fallback,
)
from app.core.stage_cache import compute_row_fingerprint


@pytest.fixture
def frames(tmp_path):
    return FrameStore(tmp_path)


def test_save_then_load_roundtrips(frames):
    table = pa.table({"a": [1, 2], "b": ["x", "y"]})
    frames.save_table("run_output", "proj/run1/stageA", table)
    assert frames.load_table("run_output", "proj/run1/stageA").equals(table)


# ── save_table/load_table are inverses ───────────────────────────────────────
# One frame carrying every cell shape the store must hand back unchanged. The
# fixtures are pandas because a literal frame reads better; what the store holds
# is the table each converts to.


def build_every_cell_shape_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "keywords": [["a", "b"], [], ["c", None]],
            "count": [1, 2, 3],
            "score": [1.5, 2.0, 3.25],
            "flag": [True, False, True],
            "name": ["x", "y", "z"],
            "seen_at": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
            "maybe_score": [1.0, None, 3.0],
        }
    )


@pytest.fixture
def round_tripped(frames):
    saved = frame_to_table(build_every_cell_shape_frame())
    frames.save_table("run_output", "proj/every_shape", saved)
    return saved, frames.load_table("run_output", "proj/every_shape")


def test_a_list_column_comes_back_as_python_lists(round_tripped):
    saved, loaded = round_tripped
    cells = loaded.column("keywords").to_pylist()
    assert [type(cell) for cell in cells] == [list, list, list]
    assert cells == saved.column("keywords").to_pylist()


@pytest.mark.parametrize("column", ["count", "score", "flag", "name", "seen_at"])
def test_a_scalar_columns_cell_types_survive_the_round_trip(round_tripped, column):
    saved, loaded = round_tripped
    assert loaded.column(column).to_pylist() == saved.column(column).to_pylist()
    assert loaded.schema.field(column).type == saved.schema.field(column).type


def test_a_null_bearing_column_comes_back_with_its_null_in_place(round_tripped):
    saved, loaded = round_tripped
    cells = loaded.column("maybe_score").to_pylist()
    assert [is_null_form(cell) for cell in cells] == [False, True, False]
    assert loaded.schema.field("maybe_score").type == saved.schema.field("maybe_score").type


def test_a_round_tripped_frame_keeps_its_frame_fingerprint(round_tripped):
    saved, loaded = round_tripped
    assert compute_table_fingerprint(loaded) == compute_table_fingerprint(saved)


def test_a_round_tripped_frames_rows_keep_their_row_fingerprints(round_tripped):
    saved, loaded = round_tripped
    assert [compute_row_fingerprint(row) for row in list_table_rows(loaded)] == [
        compute_row_fingerprint(row) for row in list_table_rows(saved)
    ]


def test_load_missing_returns_none(frames):
    assert frames.load_table("run_output", "proj/absent") is None


def test_write_once_refuses_overwrite(frames):
    df = pd.DataFrame({"a": [1]})
    frames.save_table("eval_data", "proj/set1", frame_to_table(df), overwrite=False)
    with pytest.raises(FileExistsError):
        frames.save_table("eval_data", "proj/set1", frame_to_table(df), overwrite=False)


@pytest.mark.parametrize("bad_id", ["../escape", "C:/escape", "a\\b"])
def test_unsafe_id_rejected(frames, bad_id):
    with pytest.raises(ValueError):
        frames.save_table("run_output", bad_id, pa.table({"a": [1]}))
    # validate_id must run before any path is built or written, so the location
    # that path construction would have produced never appears on disk — including
    # when a drive-absolute id would otherwise land outside frames.root entirely.
    escaped_path = frames.root / "run_output" / f"{bad_id}.parquet"
    assert not escaped_path.exists()


@pytest.mark.parametrize("bad_collection", ["../evil", "C:/escape", "a\\b"])
def test_unsafe_collection_rejected(frames, bad_collection):
    with pytest.raises(ValueError):
        frames.save_table(bad_collection, "proj/1", pa.table({"a": [1]}))
    # Same escape risk as an unsafe id (validate_id must run before any path is
    # built), just on the other path segment — so it never lands on disk either.
    escaped_path = frames.root / bad_collection / "proj/1.parquet"
    assert not escaped_path.exists()


def test_list_rows_gives_one_str_keyed_dict_per_row():
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
    """pd.isna on an array cell returns an elementwise array, not a scalar answer."""
    assert not is_null_form(cell)


def test_is_null_form_agrees_with_collapse_null_forms():
    for value in [None, float("nan"), pd.NA, pd.NaT, 0, "x", [1], np.nan]:
        assert is_null_form(value) == (collapse_null_forms(value) is None)


@pytest.mark.parametrize(
    "value",
    [None, float("nan"), pd.NA, pd.NaT, np.float32("nan"), np.datetime64("NaT", "ns")],
)
def test_is_missing_cell_accepts_every_form_including_the_two_is_null_form_misses(value):
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
    # ndarray matters: pandas' own parquet reader materializes a written list as one.
    assert is_sequence_cell(cell)


@pytest.mark.parametrize("cell", ["ab", {"a": 1}, 1, None])
def test_is_sequence_cell_rejects_scalars_strings_and_dicts(cell):
    assert not is_sequence_cell(cell)


def test_write_frame_file_round_trips_a_list_column_through_read_frame_file(tmp_path):
    frame = pd.DataFrame({"tags": [["a", "b"], []], "n": [1, 2]})
    path = tmp_path / "f.parquet"
    write_frame_file(frame, path)
    back = read_frame_file(path)
    assert [list(cell) for cell in back["tags"]] == [["a", "b"], []]
    assert compute_table_fingerprint(frame_to_table(back)) == compute_table_fingerprint(
        frame_to_table(frame))


def test_write_frame_file_writes_csv_for_a_csv_suffix(tmp_path):
    path = tmp_path / "f.csv"
    write_frame_file(pd.DataFrame({"a": [1]}), path)
    assert path.read_text(encoding="utf-8").splitlines() == ["a", "1"]


def test_write_frame_file_with_csv_fallback_takes_parquet_when_it_can(tmp_path):
    path = tmp_path / "out.parquet"
    written = write_frame_file_with_csv_fallback(pd.DataFrame({"a": [1]}), path)
    assert (written.path, written.parquet_error) == (path, None)
    assert not (tmp_path / "out.csv").exists()


def test_write_frame_file_with_csv_fallback_falls_back_and_names_the_reason(tmp_path):
    # A column mixing a dict with a list has no single arrow type, so parquet refuses it.
    frame = pd.DataFrame({"mixed": [{"a": 1}, [1, 2]]})
    written = write_frame_file_with_csv_fallback(frame, tmp_path / "out.parquet")
    assert written.path == tmp_path / "out.csv"
    assert written.parquet_error
    assert read_frame_file(written.path).shape == (2, 1)


def test_render_frame_as_csv_text_matches_what_write_frame_file_puts_on_disk(tmp_path):
    frame = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    path = tmp_path / "f.csv"
    write_frame_file(frame, path)
    assert render_frame_as_csv_text(frame) == path.read_text(encoding="utf-8")


@pytest.mark.parametrize("suffix", [".parquet", ".csv"])
def test_read_frame_column_names_matches_what_reading_the_frame_would_give(tmp_path, suffix):
    frame = pd.DataFrame({"doc_id": ["a", "b"], "flag": [True, False], "note": ["x", "y"]})
    path = tmp_path / f"frame{suffix}"
    if suffix == ".parquet":
        frame.to_parquet(path, index=False)
    else:
        frame.to_csv(path, index=False)

    assert read_frame_column_names(path) == list(read_frame_file(path).columns)
    assert read_frame_column_names(path) == ["doc_id", "flag", "note"]
