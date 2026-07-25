"""Tests for `app.core.predicate.evaluate_predicate` — the row-level evaluator
that walks a parsed predicate's syntax tree against ONE row mapping, without
`eval`, `exec`, or pandas.

The load-bearing test here is the differential one: the same grammar now has
two evaluation engines — the `pandas_expr` string that `DataFrame.eval` runs
over a whole frame, and this tree walk over a single row — and nothing but a
test keeps them answering alike. `AGREEMENT_CASES` is therefore the pin: every
construct the grammar admits, over frames that carry a null in the column each
expression reads, asserted equal to what the frame engine returns for the same
rows. A construct the tree walk cannot answer belongs in `UNSUPPORTED_CASES`,
where the assertion is that it raises rather than returns a verdict the frame
engine would disagree with."""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from app.core.errors import PredicateError
from app.core.predicate import ParsedPredicate, evaluate_predicate, parse_predicate

# Frames the case tables read. Each carries a null in every column an
# expression below evaluates, so every case exercises the null-operand rules
# rather than only the happy path. The bool and int columns are object-dtype
# deliberately: that is the dtype pandas gives a bool/int column once a null
# lands in it, and it is the only dtype whose null cell `DataFrame.eval` can
# still reduce to a boolean verdict (a nullable Int64/boolean column raises
# "cannot convert float NaN to bool" instead, so it has no verdict to agree
# with).
_NUMBERS = pd.DataFrame({"a": [0.5, 1.5, 2.0, None]})
_STRINGS = pd.DataFrame({"s": ["xy", "zz", "xa", None]})
_BOOLEANS = pd.DataFrame({"b": pd.Series([True, False, None, True], dtype=object)})
_INTEGERS = pd.DataFrame({"i": pd.Series([0, 1, None, 2], dtype=object)})
_MIXED = pd.DataFrame({
    "a": [0.5, 1.5, None, 1.5],
    "b": pd.Series([True, False, True, None], dtype=object),
})

# Every construct the grammar admits, paired with a frame whose relevant
# column carries a null. Both engines must return the same verdict per row.
AGREEMENT_CASES: list[tuple[str, pd.DataFrame]] = [
    ("a > 1", _NUMBERS),
    ("a >= 1 AND a <= 2", _NUMBERS),
    ("a > 1 OR b == False", _MIXED),
    ("a > 1 and b", _MIXED),
    ("a > 1 or b", _MIXED),
    ("not (a > 1)", _NUMBERS),
    ("1 < a < 3", _NUMBERS),
    ("a != 1", _NUMBERS),
    ("a == None", _NUMBERS),
    ("s == 'xy'", _STRINGS),
    ("s != 'xy'", _STRINGS),
    ("a IS NULL", _NUMBERS),
    ("a IS NOT NULL", _NUMBERS),
    ("s.str.contains('x')", _STRINGS),
    # A pattern with a metacharacter: pandas' str.contains is a regex search,
    # not a substring test, and a literal-substring row evaluator would answer
    # [False, False, False, False] here.
    ("s.str.contains('x|z')", _STRINGS),
    ("s.str.startswith('x')", _STRINGS),
    ("s.str.endswith('y')", _STRINGS),
    ("b", _BOOLEANS),
    ("b == true", _BOOLEANS),
    ("i", _INTEGERS),
]

# Expressions the grammar admits and `DataFrame.eval` happily evaluates, but
# the row evaluator has no implementation of. It must say so loudly: a guessed
# verdict here is exactly the drift the differential test exists to prevent.
UNSUPPORTED_CASES: list[tuple[str, pd.DataFrame]] = [
    ("s.str.upper() == 'XY'", _STRINGS),
    ("a.abs() > 1", _NUMBERS),
]

_ADMITTED_NODE_TYPES: frozenset[type[ast.AST]] = frozenset({
    ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare,
    ast.Name, ast.Constant, ast.Attribute, ast.Call,
})


@pytest.mark.parametrize("expr,frame", AGREEMENT_CASES, ids=[c[0] for c in AGREEMENT_CASES])
def test_row_evaluator_agrees_with_pandas_eval(expr: str, frame: pd.DataFrame) -> None:
    parsed = parse_predicate(expr)
    assert _evaluate_row_by_row(parsed, frame) == _evaluate_by_frame(parsed, frame)


def test_case_table_covers_every_admitted_node_type() -> None:
    covered: set[type[ast.AST]] = set()
    for expr, _frame in [*AGREEMENT_CASES, *UNSUPPORTED_CASES]:
        covered.update(type(node) for node in ast.walk(parse_predicate(expr).syntax_tree))
    assert _ADMITTED_NODE_TYPES <= covered


@pytest.mark.parametrize("expr,frame", UNSUPPORTED_CASES, ids=[c[0] for c in UNSUPPORTED_CASES])
def test_unsupported_method_raises_predicate_error(expr: str, frame: pd.DataFrame) -> None:
    method = "upper" if "upper" in expr else "abs"
    with pytest.raises(PredicateError, match=method):
        _evaluate_row_by_row(parse_predicate(expr), frame)


def test_missing_column_raises_predicate_error_naming_the_column() -> None:
    parsed = parse_predicate("missing_col > 1")
    with pytest.raises(PredicateError, match="missing_col"):
        evaluate_predicate(parsed, {"a": 1.0})


def test_non_boolean_result_raises_predicate_error() -> None:
    """A filter whose top-level value is a string is not a verdict — the frame
    engine would have refused the same expression at its `dtype=bool` step."""
    with pytest.raises(PredicateError, match="str"):
        evaluate_predicate(parse_predicate("s"), {"s": "xy"})


def test_bare_attribute_that_is_not_a_call_raises_predicate_error() -> None:
    """`s.str` parses (the grammar admits attribute access so `s.str.contains(...)`
    can), but only resolves as part of a method call."""
    with pytest.raises(PredicateError, match="str"):
        evaluate_predicate(parse_predicate("s.str"), {"s": "xy"})


@pytest.mark.parametrize("null", [None, float("nan"), pd.NA, pd.NaT])
def test_every_null_form_is_null_to_isna(null: object) -> None:
    assert evaluate_predicate(parse_predicate("a IS NULL"), {"a": null}) is True
    assert evaluate_predicate(parse_predicate("a IS NOT NULL"), {"a": null}) is False


def test_null_column_read_as_a_bare_boolean_is_false() -> None:
    """A null cell read directly as the filter's verdict is False — the same
    answer `pd.Series(column, dtype=bool)` gives that cell on the frame path
    (pinned by the `b` agreement case)."""
    assert evaluate_predicate(parse_predicate("b"), {"b": None}) is False


def test_str_method_on_a_non_text_cell_raises_predicate_error() -> None:
    """A `.str.*` test on a cell that is neither text nor null raises rather
    than inheriting the frame engine's verdict there: pandas turns that cell
    into a NaN, whose `dtype=bool` coercion is True — a match the text never
    made."""
    with pytest.raises(PredicateError, match="int"):
        evaluate_predicate(parse_predicate("s.str.contains('x')"), {"s": 5})


def test_invalid_contains_pattern_raises_predicate_error() -> None:
    """`str.contains` takes a regular expression, so an unparseable one has no
    verdict — the frame engine refuses the same pattern."""
    with pytest.raises(PredicateError, match="regular expression"):
        evaluate_predicate(parse_predicate("s.str.contains('[')"), {"s": "xy"})


def test_parsed_predicate_tree_matches_its_pandas_expr() -> None:
    """One parse feeds both engines: the tree the row evaluator walks is the
    same expression the frame engine's string runs."""
    for expr, _frame in [*AGREEMENT_CASES, *UNSUPPORTED_CASES]:
        parsed = parse_predicate(expr)
        from_tree = ast.parse(ast.unparse(parsed.syntax_tree), mode="eval")
        from_string = ast.parse(parsed.pandas_expr, mode="eval")
        assert ast.dump(from_tree) == ast.dump(from_string), expr


def test_predicate_module_never_calls_eval_or_exec() -> None:
    """The row evaluator interprets the tree itself. A filter expression is
    untrusted input: handing it to `eval`/`exec`/`compile` — or back to
    `DataFrame.eval` — would execute it instead."""
    import app.core.predicate as predicate_module

    source = Path(predicate_module.__file__).read_text(encoding="utf-8")
    called = {
        _name_of_called_function(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
    }
    assert called.isdisjoint({"eval", "exec", "compile", "query"})


def _evaluate_row_by_row(parsed: ParsedPredicate, frame: pd.DataFrame) -> list[bool]:
    return [
        evaluate_predicate(parsed, {str(key): value for key, value in record.items()})
        for record in frame.to_dict("records")
    ]


def _evaluate_by_frame(parsed: ParsedPredicate, frame: pd.DataFrame) -> list[bool]:
    """The frame engine's verdicts for the same rows, coerced exactly as the
    runtime coerces a filter mask."""
    mask = pd.Series(frame.eval(parsed.pandas_expr), index=frame.index, dtype=bool)
    return [bool(verdict) for verdict in mask.tolist()]


def _name_of_called_function(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""
