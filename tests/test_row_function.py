"""Runtime enforcement of python_row_function: the runtime maps the function over
the input's rows, so the 1:1 grain is guaranteed by the runtime, not the author."""
from __future__ import annotations

import pandas as pd
import pytest
from pydantic import ValidationError

from app.models import Stage
from app.runtime.stages import handle_python_row_function


def _stage(code, inputs=("src",)):
    return Stage.model_validate({
        "id": "t", "name": "t", "type": "python_row_function",
        "inputs": [{"id": i} for i in inputs],
        "function": {"kind": "inline", "code": code},
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


# ── Per-row error isolation (issue #71) ──────────────────────────────────────

def test_row_function_isolates_a_raising_row():
    # Row x==2 divides by zero; the other rows must survive and the failure is
    # recorded 1:1 on ctx (the runtime-internal shadow), never silently dropped.
    df = pd.DataFrame({"x": [1, 2, 3]})
    code = (
        "def transform(row):\n"
        "    return {'x': row['x'], 'y': 10 // (row['x'] - 2)}\n"
    )
    ctx: dict = {}
    out = handle_python_row_function(_stage(code), {"src": df}, ctx)

    # User-facing output has only the good rows, in order — one row short.
    assert list(out["x"]) == [1, 3]
    # The shadow is 1:1 with the 3 input rows.
    outcomes = ctx["row_errors"]["t"]
    assert [o["status"] for o in outcomes] == ["ok", "error", "ok"]
    err = outcomes[1]
    assert err["input_row"] == 1
    assert err["output_rows"] == 0
    assert err["error"]["type"] == "ZeroDivisionError"
    assert outcomes[0]["output_rows"] == 1


def test_row_function_all_rows_raise_leaves_empty_output_and_full_shadow():
    df = pd.DataFrame({"x": [1, 2]})
    code = "def transform(row):\n    raise RuntimeError('boom')\n"
    ctx: dict = {}
    out = handle_python_row_function(_stage(code), {"src": df}, ctx)
    assert len(out) == 0
    outcomes = ctx["row_errors"]["t"]
    assert [o["status"] for o in outcomes] == ["error", "error"]
    assert all(o["error"]["type"] == "RuntimeError" for o in outcomes)


def test_row_function_non_dict_return_still_fails_whole_stage():
    # A non-dict return is a systemic authoring bug (recurs for every row), so it
    # stays a whole-stage raise rather than being isolated per row.
    df = pd.DataFrame({"x": [1, 2]})
    with pytest.raises(ValueError, match="must return a dict"):
        handle_python_row_function(
            _stage("def transform(row):\n    return row['x']\n"), {"src": df}, {}
        )


def test_row_function_no_errors_records_all_ok():
    df = pd.DataFrame({"x": [1, 2]})
    code = "def transform(row):\n    return {'x': row['x']}\n"
    ctx: dict = {}
    handle_python_row_function(_stage(code), {"src": df}, ctx)
    outcomes = ctx["row_errors"]["t"]
    assert all(o["status"] == "ok" for o in outcomes)
