import pandas as pd
import pytest

from app.core.frames import FrameStore


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
