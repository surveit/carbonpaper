"""Our SQL-ish predicate dialect for `where`/`filter` expressions. The grammar is
restricted to the subset where `ast` (inspected here) and `pandas.eval`/
`DataFrame.query` (executed by the runtime) resolve column references identically.
Anything outside it — backticks, `@vars`, ... — raises `PredicateError` rather than
reaching `.eval()`/`.query()` unvalidated."""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

from app.core.errors import PredicateError

# Comparison operators the grammar admits — deliberately excludes In/NotIn/
# Is/IsNot: our dialect has no membership or identity tests.
_COMPARE_OPS: tuple[type[ast.cmpop], ...] = (
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)


@dataclass(frozen=True)
class ParsedPredicate:
    """One `where`/`filter` expression, parsed once: the columns it
    references (for a save-time check against a schema) and the pandas
    expression string to run it (for `.eval()`/`.query()` at execution time)."""
    columns: frozenset[str]
    pandas_expr: str


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
    return ParsedPredicate(columns=columns, pandas_expr=pandas_expr)


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
