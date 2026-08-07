"""`limit` caps the rows a stage READS: the handler is never handed the rest, and a
multi-input stage cuts the same window off every input."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import SubsetRunError
from app.models import parse_stage, Stage, Workflow
from app.runtime.executor import run_subset
from app.runtime.lineage import concatenated_inputs_lineage

_NAME_VAL_SCHEMA = {"columns": [{"name": "name", "type": "str", "nullable": True},
                                {"name": "val", "type": "int", "nullable": True}]}
_SEEN_SCHEMA = {"columns": [*_NAME_VAL_SCHEMA["columns"],
                            {"name": "seen", "type": "int", "nullable": True}]}

_COUNT_THE_FRAME = "def transform(df):\n    return df.assign(seen=len(df))\n"
_REFUSE_PAST_ROW_2 = (
    "def transform(row):\n"
    "    if row['val'] > 2:\n"
    "        raise ValueError('the mapper was handed a row past the cap')\n"
    "    return row\n"
)


def _rows(prefix: str, count: int, first: int = 0) -> pd.DataFrame:
    return pd.DataFrame({"name": [f"{prefix}{i}" for i in range(first, first + count)],
                         "val": list(range(first, first + count))})


def _load_stage(sid: str, df: pd.DataFrame, tmp_path) -> Stage:
    path = tmp_path / f"{sid}.csv"
    df.to_csv(path, index=False)
    return parse_stage({
        "id": sid, "name": sid, "type": "input_data",
        "connector": {"kind": "file", "params": {"path": str(path), "format": "csv"}},
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
    })


def _run(stages: list[Stage], tmp_path, name: str) -> dict[str, pd.DataFrame]:
    return run_subset(
        Workflow(stages=stages), injected_outputs={},
        stage_ids=[s.id for s in stages], run_dir=tmp_path / "runs" / name,
        repo_root=tmp_path,
    )


def test_limit_caps_the_rows_a_frame_handler_is_given(tmp_path):
    # `seen` is the row count the handler itself measured, so it reports what the
    # runtime handed over — not what survived afterwards. Capping the OUTPUT would
    # leave 3 rows all saying the handler had been given 5.
    load = _load_stage("src", _rows("s", 5), tmp_path)
    counted = parse_stage({
        "id": "counted", "name": "counted", "type": "python_frame_function",
        "inputs": [{"id": "src", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "replaces", "produces": _SEEN_SCHEMA["columns"]},
        "function": {"kind": "inline", "code": _COUNT_THE_FRAME},
        "limit": 3,
    })

    outputs = _run([load, counted], tmp_path, "frame_cap")

    assert list(outputs["counted"]["seen"]) == [3, 3, 3]
    assert list(outputs["counted"]["val"]) == [0, 1, 2]


def test_limit_keeps_the_row_mapper_off_the_rows_past_the_cap(tmp_path):
    # The mapper refuses any row past the cap, so the stage completing at all is
    # the evidence: those rows never reached it. This is the LLM fan-out claim —
    # a capped llm_transform makes N calls, it does not make them all and discard.
    load = _load_stage("src", _rows("s", 5), tmp_path)
    mapper = parse_stage({
        "id": "m", "name": "m", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "extends"},
        "function": {"kind": "inline", "code": _REFUSE_PAST_ROW_2},
        "limit": 3,
    })

    outputs = _run([load, mapper], tmp_path, "mapper_cap")

    assert list(outputs["m"]["val"]) == [0, 1, 2]


def test_the_uncapped_run_of_that_same_mapper_still_fails(tmp_path):
    # The cap is doing the work above, not a mapper that never refuses anything.
    load = _load_stage("src", _rows("s", 5), tmp_path)
    mapper = parse_stage({
        "id": "m", "name": "m", "type": "python_row_function",
        "inputs": [{"id": "src", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "extends"},
        "function": {"kind": "inline", "code": _REFUSE_PAST_ROW_2},
    })

    with pytest.raises(SubsetRunError, match="past the cap"):
        _run([load, mapper], tmp_path, "mapper_uncapped")


def test_a_limit_cuts_the_same_window_off_every_input_of_a_union(tmp_path):
    left = _load_stage("left", _rows("l", 3), tmp_path)
    right = _load_stage("right", _rows("r", 3, first=10), tmp_path)
    union = parse_stage({
        "id": "u", "name": "u", "type": "union",
        "inputs": [{"id": "left", "schema": _NAME_VAL_SCHEMA},
                   {"id": "right", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
        "union": {}, "limit": 2,
    })

    outputs = _run([left, right, union], tmp_path, "union_cap")

    # Two rows from EACH input, not the first two rows of the concatenation.
    assert list(outputs["u"]["name"]) == ["l0", "l1", "r10", "r11"]


def test_union_lineage_counts_from_the_first_row_the_stage_actually_read():
    # The runtime hands a union already-sliced frames, so their row 0 is the
    # upstream's row `first_row_ordinal` — the sidecar has to say so.
    stage = parse_stage({
        "id": "u", "name": "u", "type": "union",
        "inputs": [{"id": "left", "schema": _NAME_VAL_SCHEMA},
                   {"id": "right", "schema": _NAME_VAL_SCHEMA}],
        "signature": {"form": "replaces", "produces": _NAME_VAL_SCHEMA["columns"]},
        "union": {},
    })
    inputs = {"left": _rows("l", 2), "right": _rows("r", 2)}

    lineage = concatenated_inputs_lineage(stage, inputs, 5)

    assert [[(p.stage_id, p.row_ordinal) for p in entry] for entry in lineage.parents] == [
        [("left", 5)], [("left", 6)], [("right", 5)], [("right", 6)]
    ]
