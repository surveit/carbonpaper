"""Arm ids come from tree position, and the instrumented source runs in both interpreters."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app.core.branch_source import RECORDER_NAME, find_branch_arms, instrument_branches
from app.runtime.branches import BRANCH_SCHEMA, BranchRecorder, RowBranches
from app.runtime.code import load_function
from app.runtime.starlark_code import compile_starlark_function

ELIF_CHAIN = """
def transform(row):
    if row["a"]:
        k = "a"
    elif row["b"]:
        k = "b"
    elif row["c"]:
        k = "c"
    else:
        k = None
    return dict(row, k=k)
"""

GUARDS = """
def transform(row):
    try:
        n = int(row["n"])
    except ValueError:
        raise StepRefused("not a number")
    return dict(row, n=n)
"""


def test_an_elif_chain_names_every_arm_by_its_position() -> None:
    assert [arm.id for arm in find_branch_arms(ELIF_CHAIN)] == [
        "transform/0:if", "transform/0:elif0", "transform/0:elif1", "transform/0:else",
    ]


def test_reformatting_moves_the_line_but_not_the_id() -> None:
    spaced = ELIF_CHAIN.replace("def transform(row):", "def transform(row):\n\n    # a comment")
    before, after = find_branch_arms(ELIF_CHAIN), find_branch_arms(spaced)
    assert [a.id for a in before] == [a.id for a in after]
    assert [a.line for a in before] != [a.line for a in after]


def test_a_handler_is_an_arm_so_a_guard_that_never_fired_is_still_named() -> None:
    assert [arm.id for arm in find_branch_arms(GUARDS)] == [
        "transform/0:try", "transform/0:except0",
    ]


def test_an_arm_with_nowhere_to_insert_is_not_offered() -> None:
    assert find_branch_arms("def transform(row):\n    if row['a']: return row\n    return row") == []


def test_the_instrumented_source_opens_each_arm_with_a_recorder_call() -> None:
    text, arms = instrument_branches(ELIF_CHAIN)
    assert text.count(f'{RECORDER_NAME}("') == len(arms) == 4
    assert 'if row["a"]:\n        record_branch' in text


def _stage(name: str, key: str) -> str:
    path = Path(os.path.expanduser(
        "~/.carbonpaper/examples/venezuela_lobbying_q1_q2_2026/compiled")) / f"{name}.json"
    if not path.exists():
        pytest.skip("the stored example project is not on this machine")
    return json.loads(path.read_text(encoding="utf-8"))[key]["code"]


def test_a_python_row_function_records_one_arm_per_call_it_makes() -> None:
    recorder = BranchRecorder()
    transform = load_function(_stage("read_reported_money", "function"),
                              "transform", "transform", recorder)
    assert transform is not None
    transform({"income": "45000", "expenses": None, "filing_uuid": "f1"})
    recorder.close_row()
    collected = recorder.collected()
    assert collected is not None
    # `_read_money` runs once per money column, so one row takes two arms.
    assert collected.taken == [("_read_money/3:try", "_read_money/0:if")]


def test_a_starlark_row_function_records_through_the_real_interpreter() -> None:
    recorder = BranchRecorder()
    handle = compile_starlark_function(_stage("decide_inclusion", "starlark"),
                                       "transform", "transform", recorder)
    assert handle is not None
    for issues in ("Venezuela sanctions", "Steel tariffs"):
        handle({"issue_codes": "ENG", "specific_issues": issues, "exception_reason": None})
        recorder.close_row()
    collected = recorder.collected()
    assert collected is not None
    assert collected.taken == [("transform/3:if",), ("transform/3:else",)]


def test_no_recorder_leaves_the_authors_own_source_running() -> None:
    transform = load_function(GUARDS, "transform", "transform")
    assert transform is not None
    assert transform({"n": "7"})["n"] == 7


def test_a_branch_sidecar_carries_one_schema_however_empty(tmp_path) -> None:
    assert RowBranches([]).to_table().schema.equals(BRANCH_SCHEMA)
    populated = RowBranches([("transform/0:if",), ()]).to_table()
    assert populated.schema.equals(BRANCH_SCHEMA)
    assert RowBranches.from_table(populated).taken == [("transform/0:if",), ()]
