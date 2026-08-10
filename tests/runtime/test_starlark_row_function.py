"""The starlark_row_function handler: one compiled Starlark function mapped
over the input's rows, marshalled through Task 1's boundary."""
from __future__ import annotations

import pandas as pd
import pytest
import starlark

from app.models import parse_stage
from app.models.errors import StepRefused
from app.models.stage import StageType
from app.runtime.stages import HANDLERS, RowMapHandler
from conftest import as_inputs, rows_of, make_run_context, reads_of

DOUBLE = "def transform(row):\n    return {'n': row['n'], 'doubled': row['n'] * 2}\n"

_N_COLUMN = [{"name": "n", "type": "int", "nullable": False}]
_N_DOUBLED_SCHEMA = {"columns": [
    {"name": "n", "type": "int", "nullable": False},
    {"name": "doubled", "type": "int", "nullable": False},
]}


def _stage(code, function=None, output_schema=None, input_columns=_N_COLUMN):
    starlark = {"code": code}
    if function is not None:
        starlark["function"] = function
    return parse_stage({
        "id": "t", "description": "t", "type": "starlark_row_function",
        "inputs": [{"id": "src", "schema": {"columns": input_columns}}],
        "signature": {"form": "extends", "reads": reads_of("src", input_columns),
                      "adds": [c for c in (output_schema or {"columns": []})["columns"]
                               if c["name"] not in {i["name"] for i in input_columns}]},
        "starlark": starlark,
    })


def _handler() -> RowMapHandler:
    handler = HANDLERS[StageType.starlark_row_function]
    assert isinstance(handler, RowMapHandler)
    return handler


def test_maps_the_function_over_every_row():
    stage = _stage(DOUBLE, output_schema=_N_DOUBLED_SCHEMA)
    out = _handler().execute(stage, as_inputs({"src": pd.DataFrame({"n": [1, 2, 3]})}), make_run_context())
    assert list(rows_of(out)["doubled"]) == [2, 4, 6]


def test_starlark_cannot_reach_the_python_object_graph():
    stage = _stage("def transform(row):\n    return row.__class__\n")
    with pytest.raises(starlark.StarlarkError, match="has no attribute `__class__`"):
        _handler().execute(stage, as_inputs({"src": pd.DataFrame({"n": [1]})}), make_run_context())


def test_refuse_surfaces_as_step_refused():
    stage = _stage("def transform(row):\n    refuse('cannot adjudicate')\n")
    with pytest.raises(StepRefused, match="cannot adjudicate"):
        _handler().execute(stage, as_inputs({"src": pd.DataFrame({"n": [1]})}), make_run_context())


def test_a_function_returning_a_non_dict_fails_loudly():
    stage = _stage("def transform(row):\n    return 7\n")
    with pytest.raises(ValueError, match="dict"):
        _handler().execute(stage, as_inputs({"src": pd.DataFrame({"n": [1]})}), make_run_context())


# `starlark_marshal.MAX_EXACT_INT` is 2**63 - 1, exactly arrow's int64 ceiling, so
# with arrow as the wire format an oversized int is refused a layer EARLIER than it
# used to be: it cannot enter a frame at all, rather than being caught on the way
# into Starlark. The marshal guard stays as defence in depth for a value built
# inside the runtime, but this is where an oversized input now stops.
def test_an_oversized_int_cannot_enter_the_wire_format_at_all():
    with pytest.raises(OverflowError):
        as_inputs({"src": pd.DataFrame({"n": [2**70 + 7]})})


def test_a_row_with_a_datetime_is_marshalled_before_starlark_sees_it():
    stage = _stage(
        "def transform(row):\n    return {'iso': row['ts']}\n",
        output_schema={"columns": [{"name": "iso", "type": "str", "nullable": False}]},
        input_columns=[{"name": "ts", "type": "datetime", "nullable": False}],
    )
    out = _handler().execute(
        stage, as_inputs({"src": pd.DataFrame({"ts": [pd.Timestamp("2024-01-02T03:04:05")]})}),
        make_run_context(),
    )
    assert rows_of(out)["iso"].tolist() == ["2024-01-02T03:04:05"]


def test_an_empty_function_name_falls_back_to_transform():
    stage = _stage(DOUBLE, function="", output_schema=_N_DOUBLED_SCHEMA)
    out = _handler().execute(stage, as_inputs({"src": pd.DataFrame({"n": [5]})}), make_run_context())
    assert rows_of(out)["doubled"].tolist() == [10]


def test_the_type_is_registered_as_grain_and_order_preserving():
    assert _handler().preserves_grain_and_order is True
