"""Unit tests for the per-row read and the column-origin diff."""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.runtime.trace import _new_columns, _read_output, _row_dict
from test_trace_helpers import write_run


def test_read_output_returns_none_when_file_missing(tmp_path):
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [],
         "df": pd.DataFrame({"a": [1]})},
    ])
    (run_dir / "outputs" / "seeds.parquet").unlink()
    assert _read_output(run_dir, {"output_path": "outputs/seeds.parquet"}) is None
    assert _read_output(run_dir, {}) is None


def test_row_dict_stringifies_keys_and_delists_arrays():
    df = pd.DataFrame({"name": ["x"], "tags": [np.array(["p", "q"])]})
    assert _row_dict(df, 0) == {"name": "x", "tags": ["p", "q"]}


def test_new_columns_is_child_minus_parent():
    parent = pd.DataFrame({"facility_id": ["a"], "name": ["x"]})
    child = pd.DataFrame({"facility_id": ["a"], "name": ["x"], "score": [1]})
    assert _new_columns(child, parent) == ["score"]


def test_new_columns_all_when_no_parent():
    child = pd.DataFrame({"facility_id": ["a"], "name": ["x"]})
    assert _new_columns(child, None) == ["facility_id", "name"]
