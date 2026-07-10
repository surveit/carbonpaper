"""Runtime enforcement of python_row_function: the runtime maps the function over
the input's rows, so the 1:1 grain is guaranteed by the runtime, not the author."""
from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.runtime.stages import handle_python_row_function


def _stage(code, inputs=("src",)):
    return parse_stage({
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": i} for i in inputs],
        "function": {"kind": "inline", "code": code},
        "output_schema": {"columns": [{"name": "x", "type": "int"}]},
    })


def test_row_function_maps_per_row():
    df = pd.DataFrame({"x": [1, 2, 3]})
    code = "def transform(row):\n    return {'x': row['x'], 'y': row['x'] * 10}\n"
    out = handle_python_row_function(_stage(code), {"src": df}, {})
    assert len(out) == 3                    # 1:1 — one row out per row in
    assert list(out["y"]) == [10, 20, 30]


def test_row_function_cannot_filter_the_frame():
    # the body only ever sees a single row, so it cannot drop rows
    df = pd.DataFrame({"x": [1, 2]})
    out = handle_python_row_function(_stage("def transform(row):\n    return {'x': row['x']}\n"),
                                     {"src": df}, {})
    assert len(out) == 2


def test_row_function_empty_input():
    df = pd.DataFrame({"x": pd.Series([], dtype="int64")})
    out = handle_python_row_function(_stage("def transform(row):\n    return {'x': row['x']}\n"),
                                     {"src": df}, {})
    assert len(out) == 0


def test_row_function_rejects_non_dict_return():
    df = pd.DataFrame({"x": [1]})
    with pytest.raises(ValueError):
        handle_python_row_function(_stage("def transform(row):\n    return row['x']\n"),
                                   {"src": df}, {})


def test_row_function_rejects_multiple_inputs():
    # python_row_function's max_inputs=1 is enforced by Stage validation itself
    # (Stage._handle_for_type), so a 2-input stage can't even be constructed.
    code = "def transform(row):\n    return {'x': row['x']}\n"
    with pytest.raises(ValidationError):
        _stage(code, inputs=("a", "b"))
