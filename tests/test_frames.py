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


def test_unsafe_id_rejected(frames):
    with pytest.raises(ValueError):
        frames.save_frame("run_output", "../escape", pd.DataFrame({"a": [1]}))
