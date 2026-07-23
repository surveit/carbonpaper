"""The trace serializes to a plain nested dict a JSON response can carry."""
from __future__ import annotations

import json

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
