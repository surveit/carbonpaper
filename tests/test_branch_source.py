"""Branch ids come from tree position; the rewrite runs in both interpreters."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.core.branch_source import (
    RECORDER_NAME,
    find_branches,
    instrument_branches,
    read_branch_test,
)
from app.runtime.branches import BRANCH_SCHEMA, BranchesTaken, BranchRecorder, RowBranches
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


# ─── read_branch_test: which line a reader is pointed at ─────────────────────

_ONE_LINE_BODY = """def transform(row):
    if row["a"] == 0:
        return None
    return row
"""

_INLINE_BODY = """def transform(row):
    if row["a"] == 0: return None
    return row
"""

_MULTI_LINE_TEST = """def transform(row):
    if (row["a"] == 0
            and row["b"] > 1):
        return None
    return row
"""

_FOUR_LINE_BODY = """def transform(row):
    if row["a"] == 0:
        first = 1
        second = 2
        third = 3
        return first + second + third
    return row
"""


def _only(source: str):
    branches = find_branches(source)
    header = [b for b in branches if b.id.endswith(":if")]
    assert len(header) == 1, [b.id for b in branches]
    return header[0], source.split("\n")


def test_the_test_line_is_above_the_body() -> None:
    branch, lines = _only(_ONE_LINE_BODY)
    assert read_branch_test(lines, branch) == (2, 'if row["a"] == 0:')
    assert branch.line == 3


def test_a_one_line_if_points_at_its_own_line() -> None:
    branch, lines = _only(_INLINE_BODY)
    assert read_branch_test(lines, branch) == (2, 'if row["a"] == 0:')


def test_a_test_spanning_two_lines_is_read_whole() -> None:
    branch, lines = _only(_MULTI_LINE_TEST)
    line, text = read_branch_test(lines, branch)
    assert line == 2
    assert text == 'if (row["a"] == 0 and row["b"] > 1):'


def test_a_four_line_body_carries_its_last_line() -> None:
    # Lighting only the body's first statement says the other three never ran.
    branch, _ = _only(_FOUR_LINE_BODY)
    assert (branch.line, branch.end_line) == (3, 6)


# ─── a conditional expression: an arm with no suite to open ──────────────────

CHOICE_CHAIN = """
def transform(row):
    k = "a" if row["a"] else "b" if row["b"] else "c"
    return dict(row, k=k)
"""

FALSY_ARMS = """
def transform(row):
    return dict(row,
        a = 0 if row["p"] else 1,
        b = "" if row["p"] else "x",
        c = None if row["p"] else 1,
        d = [] if row["p"] else [1],
        e = False if row["p"] else True)
"""

COMPREHENSION = """
def transform(row):
    kept = [x for x in row["xs"] if x > 0]
    marks = [1 if x else 0 for x in row["xs"]]
    return dict(row, kept=kept, marks=marks)
"""

NESTED_CHOICE = """
def transform(row):
    k = (1 if row["c"] else 2) if row["d"] else 3
    return dict(row, k=k)
"""

# A stage in the grants workflow's shape: several groups marked, none by an `if` statement.
GROUP_MARKERS = """
WINDOW_FROM = 3
WINDOW_TO = 8

def transform(row):
    east = row["region"] == "east"
    year = row["year"]
    quarter = row["quarter"]
    in_window = east and quarter != None and quarter >= WINDOW_FROM and quarter <= WINDOW_TO
    in_years = east and year >= 2 and year <= 4
    return dict(row,
        counts_all = 1,
        counts_east = 1 if east else 0,
        counts_window = 1 if in_window else 0,
        counts_years = 1 if in_years else 0,
        counts_no_quarter = 1 if quarter == None else 0)
"""

GRANTS_BY_REASON: dict[str, dict[str, Any]] = {
    "inside the window": {"region": "east", "year": 3, "quarter": 5},
    "before the window": {"region": "east", "year": 1, "quarter": 1},
    "after the window": {"region": "east", "year": 4, "quarter": 9},
    "no quarter recorded": {"region": "east", "year": 3, "quarter": None},
    "west, so in no group": {"region": "west", "year": 3, "quarter": 5},
}


def test_a_conditional_expression_names_both_its_arms() -> None:
    assert [branch.id for branch in find_branches(CHOICE_CHAIN)] == [
        "transform/0:choice0:if", "transform/0:choice0:elif0", "transform/0:choice0:else",
    ]


def test_an_arm_reads_as_the_choice_it_was_an_arm_of() -> None:
    lines = CHOICE_CHAIN.split("\n")
    assert [read_branch_test(lines, b) for b in find_branches(CHOICE_CHAIN)] == [
        (3, "if row['a']"), (3, "elif row['b']"), (3, "else"),
    ]


def test_a_conditional_expression_records_the_arm_the_row_took() -> None:
    recorder = BranchRecorder()
    transform = load_function(CHOICE_CHAIN, "transform", "transform", recorder)
    assert transform is not None
    for index, row in enumerate(({"a": 1, "b": 0}, {"a": 0, "b": 1}, {"a": 0, "b": 0})):
        recorder.open_row(index)
        transform(row)
        recorder.close_row()
    assert recorder.branches_for(0) == ("transform/0:choice0:if",)
    assert recorder.branches_for(1) == ("transform/0:choice0:elif0",)
    assert recorder.branches_for(2) == ("transform/0:choice0:else",)


def test_an_arm_keeps_its_own_value_however_falsy_that_value_is() -> None:
    # The recorder folds in with `or`, which yields the arm only because it returns None.
    for interpreter in (load_function, compile_starlark_function):
        recorder = BranchRecorder()
        plain = interpreter(FALSY_ARMS, "transform", "transform")
        watched = interpreter(FALSY_ARMS, "transform", "transform", recorder)
        assert plain is not None and watched is not None
        for index, chose_the_first_arm in enumerate((True, False)):
            recorder.open_row(index)
            assert (watched({"p": chose_the_first_arm})
                    == plain({"p": chose_the_first_arm}))
            recorder.close_row()


def test_a_comprehension_runs_per_element_so_it_offers_no_branch() -> None:
    assert find_branches(COMPREHENSION) == []


def test_a_conditional_expression_inside_another_records_both() -> None:
    recorder = BranchRecorder()
    transform = load_function(NESTED_CHOICE, "transform", "transform", recorder)
    assert transform is not None
    for index, row in enumerate(({"c": 1, "d": 1}, {"c": 0, "d": 1}, {"c": 1, "d": 0})):
        recorder.open_row(index)
        assert transform(row)["k"] == (1, 2, 3)[index]
        recorder.close_row()
    assert recorder.branches_for(0) == ("transform/0:choice0:if", "transform/0:choice1:if")
    assert recorder.branches_for(1) == ("transform/0:choice0:if", "transform/0:choice1:else")
    assert recorder.branches_for(2) == ("transform/0:choice0:else",)


def _group_marker_arms() -> dict[str, BranchesTaken]:
    recorder = BranchRecorder()
    plain = compile_starlark_function(GROUP_MARKERS, "transform", "transform")
    watched = compile_starlark_function(GROUP_MARKERS, "transform", "transform", recorder)
    assert plain is not None and watched is not None
    taken: dict[str, BranchesTaken] = {}
    for index, (reason, row) in enumerate(GRANTS_BY_REASON.items()):
        recorder.open_row(index)
        assert watched(dict(row)) == plain(dict(row))
        recorder.close_row()
        taken[reason] = recorder.branches_for(index)
    return taken


def test_a_stage_that_decides_a_figure_without_an_if_is_no_longer_silent() -> None:
    taken = _group_marker_arms()
    window = "transform/5:choice1"
    assert (taken["inside the window"] or ())[1] == f"{window}:if"
    assert all((arms or ())[1] == f"{window}:else"
               for reason, arms in taken.items() if reason != "inside the window")


def test_each_reason_a_row_fell_outside_the_figure_took_its_own_arms() -> None:
    # Four `else` arms all reading `else`; the whole set is what tells the reasons apart.
    taken = _group_marker_arms()
    assert len(set(taken.values())) == len(GRANTS_BY_REASON)
