from __future__ import annotations

import pandas as pd

from app.models import parse_stage
from app.runtime.context import RunContext
from app.runtime.stages.aggregate import handle_aggregate
from conftest import as_inputs, place_stage, rows_of

_COLUMNS = [
    {"name": "thread", "type": "str", "nullable": False},
    {"name": "post_url", "type": "str", "nullable": True},
    {"name": "comment", "type": "str", "nullable": False},
]


def _run_aggregate(frame: pd.DataFrame, group_by: list[str]) -> pd.DataFrame:
    stage = parse_stage({
        "id": "choose", "type": "aggregate", "description": "Choose one comment row",
        "inputs": [{"id": "comments"}],
        "signature": {
            "form": "replaces",
            "reads": [{"input": "comments", "columns": _COLUMNS}],
            "produces": [
                *([_COLUMNS[0]] if group_by else []),
                {"name": "first_present_url", "type": "str", "nullable": True},
                {"name": "first_row_url", "type": "str", "nullable": True},
                {"name": "first_row_comment", "type": "str", "nullable": True},
            ],
        },
        "aggregate": {"group_by": group_by, "aggregations": [
            {"output_column": "first_present_url", "formula": "first",
             "value_column": "post_url"},
            {"output_column": "first_row_url", "formula": "first_row",
             "value_column": "post_url"},
            {"output_column": "first_row_comment", "formula": "first_row",
             "value_column": "comment"},
        ]},
    })
    ctx = RunContext.for_stages_outside_a_run(run_dir=None)
    return rows_of(handle_aggregate(
        place_stage(stage), as_inputs({"comments": frame}), ctx))


def test_first_row_keeps_null_and_sibling_values_from_the_same_row():
    rows = pd.DataFrame({
        "thread": ["one", "one"],
        "post_url": [None, "B"],
        "comment": ["A", "C"],
    })

    result = _run_aggregate(rows, ["thread"]).iloc[0]

    assert result["first_present_url"] == "B"
    assert pd.isna(result["first_row_url"])
    assert result["first_row_comment"] == "A"


def test_first_row_observes_input_order():
    rows = pd.DataFrame({
        "thread": ["one", "one"],
        "post_url": [None, "B"],
        "comment": ["A", "C"],
    })

    result = _run_aggregate(rows.iloc[::-1].reset_index(drop=True), ["thread"]).iloc[0]

    assert result["first_row_url"] == "B"
    assert result["first_row_comment"] == "C"


def test_first_row_keeps_an_all_null_group_null():
    rows = pd.DataFrame({
        "thread": ["one", "one"],
        "post_url": [None, None],
        "comment": ["A", "C"],
    })

    result = _run_aggregate(rows, ["thread"]).iloc[0]

    assert pd.isna(result["first_present_url"])
    assert pd.isna(result["first_row_url"])


def test_first_row_keeps_position_for_a_whole_frame_reduction():
    rows = pd.DataFrame({
        "thread": ["one", "one"],
        "post_url": [None, "B"],
        "comment": ["A", "C"],
    })

    result = _run_aggregate(rows, []).iloc[0]

    assert result["first_present_url"] == "B"
    assert pd.isna(result["first_row_url"])
    assert result["first_row_comment"] == "A"
