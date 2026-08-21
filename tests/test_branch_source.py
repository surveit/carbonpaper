"""Branch ids come from tree position; the rewrite runs in both interpreters."""
from __future__ import annotations

from pathlib import Path

from app.core.branch_source import RECORDER_NAME, find_branches, instrument_branches
from app.runtime.branches import BRANCH_SCHEMA, BranchRecorder, RowBranches
from app.runtime.code import load_function
from app.runtime.starlark_code import compile_starlark_function

# Verbatim from the venezuela_lobbying_q1_q2_2026 example.
_STAGE_CODE = Path(__file__).resolve().parent / "fixtures" / "stage_code"

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

ONE_LINER = """
def transform(row):
    if row["a"]: k = "a"
    else: k = "b"
    return dict(row, k=k)
"""


def stage_code(name: str) -> str:
    return (_STAGE_CODE / f"{name}.txt").read_text(encoding="utf-8")


def test_an_elif_chain_names_every_branch_by_its_position() -> None:
    assert [branch.id for branch in find_branches(ELIF_CHAIN)] == [
        "transform/0:if", "transform/0:elif0", "transform/0:elif1", "transform/0:else",
    ]


def test_reformatting_moves_the_line_but_not_the_id() -> None:
    spaced = ELIF_CHAIN.replace("def transform(row):", "def transform(row):\n\n    # a comment")
    before, after = find_branches(ELIF_CHAIN), find_branches(spaced)
    assert [a.id for a in before] == [a.id for a in after]
    assert [a.line for a in before] != [a.line for a in after]


def test_a_handler_is_a_branch_so_a_guard_that_never_fired_is_still_named() -> None:
    assert [branch.id for branch in find_branches(stage_code("read_reported_money"))] == [
        "_read_money/0:if", "_read_money/2:if", "_read_money/3:try",
        "_read_money/3:except0", "_read_money/4:if",
    ]


def test_a_branch_sharing_its_header_line_is_still_offered() -> None:
    assert [branch.id for branch in find_branches(ONE_LINER)] == [
        "transform/0:if", "transform/0:else",
    ]


def test_a_one_line_branch_records_like_any_other() -> None:
    recorder = BranchRecorder()
    transform = load_function(ONE_LINER, "transform", "transform", recorder)
    assert transform is not None
    for index, a in enumerate((1, 0)):
        recorder.open_row(index)
        transform({"a": a})
        recorder.close_row()
    assert recorder.branches_for(0) == ("transform/0:if",)
    assert recorder.branches_for(1) == ("transform/0:else",)


def test_a_predicate_with_no_statement_branch_offers_none() -> None:
    assert find_branches(stage_code("venezuela_rows")) == []


def test_instrumenting_changes_which_branches_are_seen_and_nothing_else() -> None:
    """The claim the whole design rests on: the rewrite observes, it does not alter."""
    rows = [{"a": 1, "b": 0, "c": 0}, {"a": 0, "b": 1, "c": 0},
            {"a": 0, "b": 0, "c": 1}, {"a": 0, "b": 0, "c": 0}]
    plain = load_function(ELIF_CHAIN, "transform", "transform")
    recorder = BranchRecorder()
    watched = load_function(ELIF_CHAIN, "transform", "transform", recorder)
    assert plain is not None and watched is not None
    observed = []
    for index, row in enumerate(rows):
        recorder.open_row(index)
        observed.append(watched(dict(row)))
        recorder.close_row()
    assert [plain(dict(row)) for row in rows] == observed


def test_the_rewrite_opens_each_branch_exactly_once() -> None:
    text, branches = instrument_branches(ELIF_CHAIN)
    assert text.count(f'{RECORDER_NAME}("') == len(branches) == 4


def test_a_python_row_function_records_one_branch_per_call_it_makes() -> None:
    recorder = BranchRecorder()
    transform = load_function(stage_code("read_reported_money"), "transform", "transform", recorder)
    assert transform is not None
    recorder.open_row(0)
    transform({"income": "45000", "expenses": None, "filing_uuid": "f1"})
    recorder.close_row()
    # `_read_money` runs once per money column, so one row takes two branches.
    assert recorder.branches_for(0) == ("_read_money/3:try", "_read_money/0:if")


def test_a_starlark_row_function_records_through_the_real_interpreter() -> None:
    recorder = BranchRecorder()
    handle = compile_starlark_function(
        stage_code("decide_inclusion"), "transform", "transform", recorder)
    assert handle is not None
    for index, issues in enumerate(("Venezuela sanctions", "Steel tariffs")):
        recorder.open_row(index)
        handle({"issue_codes": "ENG", "specific_issues": issues, "exception_reason": None})
        recorder.close_row()
    assert recorder.branches_for(0) == ("transform/3:if",)
    assert recorder.branches_for(1) == ("transform/3:else",)
    assert recorder.branches_for(2) is None


def test_a_starlark_stage_computes_the_same_values_instrumented_or_not() -> None:
    code = stage_code("split_paid_from_in_house")
    row = {"type": "1st Quarter - Report", "registrant_org": "A LLC", "client_org": "B Corp"}
    recorder = BranchRecorder()
    plain = compile_starlark_function(code, "transform", "transform")
    watched = compile_starlark_function(code, "transform", "transform", recorder)
    assert plain is not None and watched is not None
    recorder.open_row(0)
    assert plain(dict(row)) == watched(dict(row))
    recorder.close_row()


def test_no_recorder_leaves_the_authors_own_source_running() -> None:
    transform = load_function(ELIF_CHAIN, "transform", "transform")
    assert transform is not None
    assert transform({"a": 1, "b": 0, "c": 0})["k"] == "a"


def test_a_branch_sidecar_carries_one_schema_however_empty() -> None:
    assert RowBranches([]).to_table().schema.equals(BRANCH_SCHEMA)
    populated = RowBranches([("transform/0:if",), (), None]).to_table()
    assert populated.schema.equals(BRANCH_SCHEMA)
    assert RowBranches.from_table(populated).taken == [("transform/0:if",), (), None]
