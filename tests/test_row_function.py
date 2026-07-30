"""Runtime enforcement of python_row_function: the runtime maps the function over
the input's rows, so the 1:1 grain is guaranteed by the runtime, not the author."""
from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from app.models import parse_stage
from app.runtime.stages import HANDLERS
from conftest import make_run_context


# Every frame below is a single int column `x`; `output_columns` names what the
# `code` under test returns.
_X_COLUMN = [{"name": "x", "type": "int"}]


def _stage(code, inputs=("src",), output_columns=_X_COLUMN):
    return parse_stage({
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": i, "schema": {"columns": _X_COLUMN}} for i in inputs],
        "output_schema": {"columns": output_columns},
        "function": {"kind": "inline", "code": code},
    })


def _run(stage, frames):
    return HANDLERS[stage.type].execute(stage, frames, make_run_context())


def test_row_function_maps_per_row():
    df = pd.DataFrame({"x": [1, 2, 3]})
    code = "def transform(row):\n    return {'x': row['x'], 'y': row['x'] * 10}\n"
    out = _run(_stage(code, output_columns=_X_COLUMN + [{"name": "y", "type": "int"}]),
               {"src": df})
    assert len(out) == 3                    # 1:1 — one row out per row in
    assert list(out["y"]) == [10, 20, 30]


def test_row_function_cannot_filter_the_frame():
    # the body only ever sees a single row, so it cannot drop rows
    df = pd.DataFrame({"x": [1, 2]})
    out = _run(_stage("def transform(row):\n    return {'x': row['x']}\n"), {"src": df})
    assert len(out) == 2


def test_row_function_empty_input():
    df = pd.DataFrame({"x": pd.Series([], dtype="int64")})
    out = _run(_stage("def transform(row):\n    return {'x': row['x']}\n"), {"src": df})
    assert len(out) == 0


def test_row_function_rejects_non_dict_return():
    df = pd.DataFrame({"x": [1]})
    with pytest.raises(ValueError):
        _run(_stage("def transform(row):\n    return row['x']\n"), {"src": df})


def test_row_function_rejects_multiple_inputs():
    # python_row_function's max_inputs=1 is enforced by Stage validation itself
    # (PythonRowFunctionStage declares inputs max_length=1), so a 2-input stage
    # can't even be constructed.
    code = "def transform(row):\n    return {'x': row['x']}\n"
    with pytest.raises(ValidationError):
        _stage(code, inputs=("a", "b"))
