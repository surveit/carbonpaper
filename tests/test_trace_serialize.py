"""The trace serializes to a plain nested dict a JSON response can carry."""
from __future__ import annotations

import json
import math

import pandas as pd

from app.runtime.trace import trace_row, trace_to_dict
from test_trace_helpers import write_run


def test_trace_to_dict_is_json_roundtrippable(tmp_path):
    seeds = pd.DataFrame({"facility_id": ["a", "b"], "name": ["A", "B"]})
    enrich = seeds.assign(score=[1, 2])
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    payload = trace_to_dict(trace_row(run_dir, "enrich", 0))
    # Must survive a JSON round-trip unchanged.
    assert json.loads(json.dumps(payload)) == payload
    assert payload["end"]["reached_origin"] is True
    assert payload["steps"][0]["stage_id"] == "enrich"
    assert payload["steps"][0]["columns_new"] == ["score"]
    assert payload["steps"][0]["row"]["name"] == "A"


def test_trace_to_dict_turns_non_finite_floats_into_null(tmp_path):
    # A nullable numeric column (e.g. income, legitimately absent on most
    # rows) arrives from pandas as NaN; +-inf is possible from a computed
    # column too. None of the three are valid JSON tokens.
    seeds = pd.DataFrame({"facility_id": ["a", "b", "c"]})
    enrich = seeds.assign(income=[math.nan, math.inf, -math.inf], expenses=[0, 0.0, 5])
    run_dir = write_run(tmp_path, [
        {"id": "seeds", "type": "input_data", "parents": [], "df": seeds},
        {"id": "enrich", "type": "python_row_function", "parents": ["seeds"], "df": enrich},
    ])
    rows = [
        trace_to_dict(trace_row(run_dir, "enrich", r))["steps"][0]["row"]
        for r in range(3)
    ]
    assert [row["income"] for row in rows] == [None, None, None]
    # A genuine zero must survive untouched, never conflated with "missing".
    assert rows[0]["expenses"] == 0
    assert rows[0]["expenses"] is not None
    payload = json.dumps(rows)
    assert json.loads(payload) == rows
