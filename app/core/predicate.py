"""Our SQL-ish predicate dialect, shared by the runtime stage handlers that
evaluate a `where`/`filter` expression against data (aggregate,
human_review_queue) and by the save-time validator that checks such an
expression only references columns that exist.

`parse_predicate` does ONE parse and returns everything the three consumers
need: the referenced columns, the pandas expression string, and the validated
syntax tree. The grammar is restricted to exactly the subset where `ast` (what
this module inspects) and `pandas.eval`/`DataFrame.query` (what a frame-level
caller executes) resolve column references identically. Anything outside that
grammar raises `PredicateError` rather than being forwarded to pandas
unchecked — a construct `ast` and pandas could read differently (backticks,
`@vars`, ...) is exactly what must never reach `.eval()`/`.query()`
unvalidated.

There are two evaluation surfaces, both fed by that one parse:

- `.pandas_expr`, a string a caller runs over a whole frame with
  `.eval()`/`.query()`;
- `evaluate_predicate`, which walks `.syntax_tree` against a single row
  mapping — no `eval`, no `exec`, no pandas expression engine — for a caller
  that only ever sees one row at a time.

Because validation (which reads `.columns`) and both evaluators come from the
same parse, a filter that validation accepts is exactly one either evaluator
can run. The two evaluators agreeing on a verdict is not free, though: it is
held by the differential test in
`tests/core/test_predicate_row_evaluation.py`, which asserts row-by-row
equality with `DataFrame.eval` over every construct the grammar admits. A
construct the row walk cannot answer raises `PredicateError` rather than
returning a verdict that could differ."""
from __future__ import annotations

import ast
import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import numpy as np
import pandas as pd

from app.core.errors import PredicateError

# The comparison operators the grammar admits, each with the Python function
# the row evaluator applies for it. One table so the parser's admitted set and
# the row evaluator's implemented set cannot drift apart. Deliberately excludes
# In/NotIn/Is/IsNot: our dialect has no membership or identity tests.
_COMPARISONS: dict[type[ast.cmpop], Callable[..., object]] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
_COMPARE_OPS: tuple[type[ast.cmpop], ...] = tuple(_COMPARISONS)

# The `col.str.<method>(<literal>)` tests the row evaluator implements.
_STRING_METHODS: frozenset[str] = frozenset({"contains", "startswith", "endswith"})

# The `col.<method>()` null tests, each with the verdict it returns for a null
# cell (its verdict for a non-null cell is the negation).
_NULL_TEST_METHODS: dict[str, bool] = {"isna": True, "notna": False}


@dataclass(frozen=True)
class ParsedPredicate:
    """One `where`/`filter` expression, parsed once, in the three shapes its
    consumers need: `columns`, the column names it references (for a save-time
    check against a schema); `pandas_expr`, the expression string to run it
    over a frame with `.eval()`/`.query()`; and `syntax_tree`, the validated
    `ast` body node `evaluate_predicate` walks to run it against one row."""
    columns: frozenset[str]
    pandas_expr: str
    syntax_tree: ast.expr


def parse_predicate(expr: str) -> ParsedPredicate:
    """Parse one `where`/`filter` expression in our SQL-ish dialect into a
    `ParsedPredicate`.

    Normalizes the SQL-ish surface to a pandas expression, parses THAT string
    with `ast` in eval mode, and walks the tree against a closed grammar:
    boolean combinators, comparisons, `&`/`|`, `not`, bare column names,
    literals, attribute access, and method calls on a column (`col.isna()`,
    `col.str.contains('x')`). Raises `PredicateError` if `expr` doesn't even
    parse as Python (backticks, `@vars`, and other non-Python surface — the
    constructs where `ast` and `pandas.eval` could diverge, so rejecting them
    is the point) or if the tree contains a node outside that grammar (a bare
    function call, arithmetic, subscripting, a comprehension, ...)."""
    pandas_expr = _normalize(expr)
    try:
        tree = ast.parse(pandas_expr, mode="eval")
    except SyntaxError as exc:
        raise PredicateError(
            f"filter is not valid: {expr!r} (not parseable as a Python expression: {exc})"
        ) from exc
    _validate_node(tree.body, expr)
    columns = frozenset(
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    return ParsedPredicate(columns=columns, pandas_expr=pandas_expr, syntax_tree=tree.body)


def evaluate_predicate(parsed: ParsedPredicate, row: Mapping[str, object]) -> bool:
    """Evaluate an already-parsed predicate against ONE row: its verdict for
    that row alone, `True` meaning the filter matched.

    Walks `parsed.syntax_tree` — the tree the parse already validated — and
    interprets each node against `row`, whose keys are column names. Nothing is
    compiled or executed: no `eval`, no `exec`, no pandas expression engine.

    Null cells follow the frame engine's own answers: a comparison with a null
    operand is `True` for `!=` and `False` for every other operator (never a
    `TypeError`), a `str.*` test on a null cell is `False`, and a null read
    directly as the whole verdict is `False`.

    Raises `PredicateError`, and nothing else, when the row cannot yield a
    verdict: a referenced column is absent, a method outside the supported set
    (`isna`, `notna`, `str.contains`, `str.startswith`, `str.endswith`) is
    called, or the expression's value is not a true/false verdict. It never
    guesses a verdict for something it cannot evaluate."""
    value = _evaluate_node(parsed.syntax_tree, row, parsed.pandas_expr)
    return _coerce_to_bool(value, parsed.pandas_expr)


def _normalize(expr: str) -> str:
    """Translate our SQL-ish predicate dialect to pandas eval syntax.

    Wraps AND/OR operands in parens so bitwise &/| binds the right way,
    and lowercases boolean literals."""
    e = expr
    e = e.replace(" IS NOT NULL", ".notna()")
    e = e.replace(" IS NULL", ".isna()")
    e = re.sub(r"\btrue\b", "True", e, flags=re.IGNORECASE)
    e = re.sub(r"\bfalse\b", "False", e, flags=re.IGNORECASE)

    def _split_wrap(s: str, sep: str, joiner: str) -> str:
        parts = [p.strip() for p in re.split(rf"\s+{sep}\s+", s)]
        if len(parts) <= 1:
            return s
        return f" {joiner} ".join(f"({p})" for p in parts)

    e = _split_wrap(e, "OR", "|")
    e = _split_wrap(e, "AND", "&")
    return e


def _validate_node(node: ast.AST, expr: str) -> None:
    """Raise `PredicateError` unless every node in this expression subtree is
    one where `ast` and pandas agree on column resolution: boolean
    combinators, `&`/`|`, `not`, comparisons, bare names, literals, attribute
    access, and method calls on a column. Recurses only into the child nodes
    each allowed construct can legitimately hold — anything else (a bare-name
    function call, arithmetic, subscripting, a comprehension, ...) is rejected
    by name by the final `else` rather than silently admitted."""
    if isinstance(node, ast.BoolOp):
        _validate_bool_op(node, expr)
    elif isinstance(node, ast.BinOp):
        _validate_bin_op(node, expr)
    elif isinstance(node, ast.UnaryOp):
        _validate_unary_op(node, expr)
    elif isinstance(node, ast.Compare):
        _validate_compare(node, expr)
    elif isinstance(node, ast.Name):
        _validate_name(node, expr)
    elif isinstance(node, ast.Constant):
        pass
    elif isinstance(node, ast.Attribute):
        _validate_node(node.value, expr)
    elif isinstance(node, ast.Call):
        _validate_call(node, expr)
    else:
        raise PredicateError(
            f"filter is not valid: {expr!r} (`{type(node).__name__}` is not supported in a filter expression)"
        )


def _validate_bool_op(node: ast.BoolOp, expr: str) -> None:
    if not isinstance(node.op, (ast.And, ast.Or)):
        raise PredicateError(
            f"filter is not valid: {expr!r} (boolean operator `{type(node.op).__name__}` is not supported)"
        )
    for value in node.values:
        _validate_node(value, expr)


def _validate_bin_op(node: ast.BinOp, expr: str) -> None:
    if not isinstance(node.op, (ast.BitAnd, ast.BitOr)):
        raise PredicateError(
            f"filter is not valid: {expr!r} (operator `{type(node.op).__name__}` is not supported; "
            "only AND/OR combine sub-expressions)"
        )
    _validate_node(node.left, expr)
    _validate_node(node.right, expr)


def _validate_unary_op(node: ast.UnaryOp, expr: str) -> None:
    if not isinstance(node.op, ast.Not):
        raise PredicateError(
            f"filter is not valid: {expr!r} (unary operator `{type(node.op).__name__}` is not supported; "
            "only NOT is)"
        )
    _validate_node(node.operand, expr)


def _validate_compare(node: ast.Compare, expr: str) -> None:
    for op in node.ops:
        if not isinstance(op, _COMPARE_OPS):
            raise PredicateError(
                f"filter is not valid: {expr!r} (comparison `{type(op).__name__}` is not supported)"
            )
    _validate_node(node.left, expr)
    for comparator in node.comparators:
        _validate_node(comparator, expr)


def _validate_name(node: ast.Name, expr: str) -> None:
    if not isinstance(node.ctx, ast.Load):
        raise PredicateError(
            f"filter is not valid: {expr!r} (name `{node.id}` is not a plain read reference)"
        )


def _validate_call(node: ast.Call, expr: str) -> None:
    if not isinstance(node.func, ast.Attribute):
        raise PredicateError(
            f"filter is not valid: {expr!r} (a function call is not supported; only a method call on "
            "a column, like col.isna() or col.str.contains(...), is)"
        )
    if node.keywords:
        raise PredicateError(
            f"filter is not valid: {expr!r} (keyword arguments in a method call are not supported)"
        )
    _validate_node(node.func, expr)
    for arg in node.args:
        _validate_node(arg, expr)


# --- evaluate_predicate: the row-level walk over an already-validated tree ---


def _evaluate_node(node: ast.expr, row: Mapping[str, object], expr: str) -> object:
    """This node's value for `row`, dispatching per construct in the same
    shape `_validate_node` dispatches — so a construct the grammar admits has
    exactly one evaluation branch here. Interior values are returned as they
    are (a cell's own type, a literal, a sub-expression's boolean); only the
    boolean combinators and the final verdict coerce. Anything the dispatch
    does not name raises `PredicateError` rather than falling through to a
    guessed value; the parse rejects those first, so reaching this is a bug,
    not a user error."""
    if isinstance(node, ast.BoolOp):
        return _evaluate_bool_op(node, row, expr)
    if isinstance(node, ast.BinOp):
        return _evaluate_bin_op(node, row, expr)
    if isinstance(node, ast.UnaryOp):
        return _evaluate_unary_op(node, row, expr)
    if isinstance(node, ast.Compare):
        return _evaluate_compare(node, row, expr)
    if isinstance(node, ast.Name):
        return _read_cell(node, row, expr)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Call):
        return _evaluate_call(node, row, expr)
    if isinstance(node, ast.Attribute):
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} (`{ast.unparse(node)}` is an attribute, "
            "which has a value only as the method of a call like col.isna() or col.str.contains(...))"
        )
    raise PredicateError(
        f"filter cannot be evaluated row by row: {expr!r} "
        f"(`{type(node).__name__}` has no row-level evaluation)"
    )


def _evaluate_bool_op(node: ast.BoolOp, row: Mapping[str, object], expr: str) -> bool:
    """`and`/`or` over the coerced booleans of every operand. Every operand is
    evaluated (no short-circuit), matching the frame engine, which evaluates
    both sides elementwise — so an operand that cannot be evaluated raises
    whichever side of the combinator it sits on."""
    operands = [_coerce_to_bool(_evaluate_node(value, row, expr), expr) for value in node.values]
    if isinstance(node.op, ast.And):
        return all(operands)
    if isinstance(node.op, ast.Or):
        return any(operands)
    raise PredicateError(
        f"filter cannot be evaluated row by row: {expr!r} "
        f"(boolean operator `{type(node.op).__name__}` has no row-level evaluation)"
    )


def _evaluate_bin_op(node: ast.BinOp, row: Mapping[str, object], expr: str) -> bool:
    """`&`/`|` — what the SQL-ish `AND`/`OR` normalizes to — combined exactly
    as `and`/`or` are, over the coerced booleans of both sides."""
    left = _coerce_to_bool(_evaluate_node(node.left, row, expr), expr)
    right = _coerce_to_bool(_evaluate_node(node.right, row, expr), expr)
    if isinstance(node.op, ast.BitAnd):
        return left and right
    if isinstance(node.op, ast.BitOr):
        return left or right
    raise PredicateError(
        f"filter cannot be evaluated row by row: {expr!r} "
        f"(operator `{type(node.op).__name__}` has no row-level evaluation)"
    )


def _evaluate_unary_op(node: ast.UnaryOp, row: Mapping[str, object], expr: str) -> bool:
    if not isinstance(node.op, ast.Not):
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} "
            f"(unary operator `{type(node.op).__name__}` has no row-level evaluation)"
        )
    return not _coerce_to_bool(_evaluate_node(node.operand, row, expr), expr)


def _evaluate_compare(node: ast.Compare, row: Mapping[str, object], expr: str) -> bool:
    """A comparison, or a chain of them (`1 < a < 3`): every operand is
    evaluated once, each adjacent pair compared, and the verdicts combined
    with `and`. No pair is skipped once one is False — the frame engine
    evaluates the whole chain elementwise too, so an operand that cannot be
    evaluated raises wherever it sits in the chain."""
    operands = [_evaluate_node(operand, row, expr) for operand in [node.left, *node.comparators]]
    verdicts = [
        _compare_one_pair(left, op, right, expr)
        for left, op, right in zip(operands[:-1], node.ops, operands[1:], strict=True)
    ]
    return all(verdicts)


def _compare_one_pair(left: object, op: ast.cmpop, right: object, expr: str) -> bool:
    """One comparison's verdict.

    A null operand on either side makes the verdict `True` for `!=` and
    `False` for every other operator — never a `TypeError` — matching what
    numpy/pandas return for the same comparison against a null cell. Two
    values Python refuses to compare (a string against a number) raise
    `PredicateError` rather than escaping as a `TypeError`."""
    if _is_null(left) or _is_null(right):
        return isinstance(op, ast.NotEq)
    compare = _COMPARISONS.get(type(op))
    if compare is None:
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} "
            f"(comparison `{type(op).__name__}` has no row-level evaluation)"
        )
    try:
        return bool(compare(left, right))
    except TypeError as exc:
        raise PredicateError(
            f"filter cannot be evaluated for this row: {expr!r} (comparing a "
            f"`{type(left).__name__}` with a `{type(right).__name__}` is not possible: {exc})"
        ) from exc


def _read_cell(node: ast.Name, row: Mapping[str, object], expr: str) -> object:
    """The row's cell for this column name. A column the row does not carry
    raises, naming it and the columns the row does carry — the row cannot say
    whether the filter matched, and answering `False` would silently exclude
    every row over a typo."""
    if node.id not in row:
        raise PredicateError(
            f"filter cannot be evaluated for this row: {expr!r} (column `{node.id}` is not in the row, "
            f"which has: {sorted(row)})"
        )
    return row[node.id]


def _evaluate_call(node: ast.Call, row: Mapping[str, object], expr: str) -> bool:
    """A method call on a column: `col.isna()`, `col.notna()`, or one of
    `col.str.contains/startswith/endswith(<literal>)`. Any other method — one
    the frame engine would happily run, like `col.abs()` or `col.str.upper()`
    — raises, naming it and what it was called on."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} (only a method call on a column, "
            "like col.isna() or col.str.contains(...), has a row-level evaluation)"
        )
    if func.attr in _NULL_TEST_METHODS:
        return _evaluate_null_test(func, node, row, expr)
    if func.attr in _STRING_METHODS:
        return _evaluate_string_test(func, node, row, expr)
    raise PredicateError(
        f"filter cannot be evaluated row by row: {expr!r} (method `{func.attr}()` on "
        f"`{ast.unparse(func.value)}` has no row-level evaluation; supported methods are "
        "isna(), notna(), str.contains(...), str.startswith(...), str.endswith(...))"
    )


def _evaluate_null_test(
    func: ast.Attribute, node: ast.Call, row: Mapping[str, object], expr: str
) -> bool:
    """`col.isna()` / `col.notna()` for this row's cell."""
    if node.args:
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} (`{func.attr}()` takes no arguments)"
        )
    column = _require_column_name(func.value, func.attr, expr)
    cell_is_null = _is_null(_read_cell(column, row, expr))
    return cell_is_null == _NULL_TEST_METHODS[func.attr]


def _evaluate_string_test(
    func: ast.Attribute, node: ast.Call, row: Mapping[str, object], expr: str
) -> bool:
    """`col.str.contains/startswith/endswith(<literal>)` for this row's cell.

    A null cell is `False` — the same verdict the frame engine gives a null
    cell for these methods. A cell that is neither null nor a string raises:
    the method has no meaning for it, and a `False` there would read as "the
    text did not match" rather than "this was never text"."""
    accessor = func.value
    if not isinstance(accessor, ast.Attribute) or accessor.attr != "str":
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} (`{func.attr}(...)` is supported only on a "
            f"column's .str accessor, not on `{ast.unparse(accessor)}`)"
        )
    column = _require_column_name(accessor.value, func.attr, expr)
    argument = _require_string_literal(node, func.attr, expr)
    cell = _read_cell(column, row, expr)
    if _is_null(cell):
        return False
    if not isinstance(cell, str):
        raise PredicateError(
            f"filter cannot be evaluated for this row: {expr!r} (`{column.id}` holds a "
            f"`{type(cell).__name__}`, which has no .str.{func.attr}())"
        )
    return _apply_string_method(func.attr, cell, argument, expr)


def _apply_string_method(method: str, cell: str, argument: str, expr: str) -> bool:
    """One `str.*` test's verdict for a non-null text cell, each matching the
    frame engine's own semantics for that method: `contains` searches `cell`
    for `argument` as a REGULAR EXPRESSION (pandas compiles the pattern unless
    told otherwise, so `contains('a|b')` is an alternation there and must be
    one here), while `startswith`/`endswith` test literal text."""
    if method == "contains":
        return _search_for_pattern(argument, cell, expr)
    if method == "startswith":
        return cell.startswith(argument)
    if method == "endswith":
        return cell.endswith(argument)
    raise PredicateError(
        f"filter cannot be evaluated row by row: {expr!r} "
        f"(text method `{method}(...)` has no row-level evaluation)"
    )


def _search_for_pattern(pattern: str, cell: str, expr: str) -> bool:
    try:
        return re.search(pattern, cell) is not None
    except re.error as exc:
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} "
            f"(contains({pattern!r}) is not a valid regular expression: {exc})"
        ) from exc


def _require_column_name(node: ast.expr, method: str, expr: str) -> ast.Name:
    """The bare column name a method was called on."""
    if not isinstance(node, ast.Name):
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} (`{method}` is supported only on a column, "
            f"not on `{ast.unparse(node)}`)"
        )
    return node


def _require_string_literal(node: ast.Call, method: str, expr: str) -> str:
    """The single string literal a `str.*` method was called with."""
    if len(node.args) != 1:
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} "
            f"(`{method}(...)` takes exactly one argument, got {len(node.args)})"
        )
    argument = node.args[0]
    if not isinstance(argument, ast.Constant) or not isinstance(argument.value, str):
        raise PredicateError(
            f"filter cannot be evaluated row by row: {expr!r} "
            f"(`{method}(...)` takes a text literal, not `{ast.unparse(argument)}`)"
        )
    return argument.value


def _is_null(value: object) -> bool:
    """True if `value` is one of the four null forms a row cell can carry:
    plain `None`, `float('nan')`, `pd.NA`, or `pd.NaT`. Each is tested
    individually — an identity check for None/pd.NA/pd.NaT, an explicit
    isinstance+isnan for a float nan — rather than via a single `pd.isna`
    call, whose stubs do not accept a bare `object` and which returns an
    elementwise array (ambiguous in an `if`) for an array-valued cell. An
    array-valued cell matches none of these checks and is simply not null."""
    if value is None or value is pd.NA or value is pd.NaT:
        return True
    return isinstance(value, float) and math.isnan(value)


def _coerce_to_bool(value: object, expr: str) -> bool:
    """`value` as a true/false verdict, coerced exactly as the frame engine's
    `pd.Series(column, dtype=bool)` step coerces the same cell: a boolean
    stays itself, a number is truthy when non-zero, and a plain `None` — the
    null form an object column carries — is False. Every other type, `pd.NA`
    and `pd.NaT` among them (the frame engine refuses those too), raises
    rather than being forced into a verdict."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if value is None:
        return False
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(value)
    raise PredicateError(
        f"filter did not evaluate to a true/false verdict for this row: {expr!r} "
        f"(it produced a `{type(value).__name__}`)"
    )
