"""Our SQL-ish predicate dialect for `where`/`filter` expressions: a closed set of AST
node types AND a closed allowlist of attribute names. It admits only constructs where
`ast` (inspected here) and `pandas.eval`/`DataFrame.query` (executed by the runtime)
agree on column resolution, and no chain an author can write reaches past a column.
Anything else raises `PredicateError` rather than reaching `.eval()`/`.query()`."""
from __future__ import annotations

import ast
import keyword
import re
from dataclasses import dataclass
from typing import Collection, Mapping

from app.core.errors import PredicateError

# A quoted name is an allowlist lookup against the caller's columns.
_BACKTICK_QUOTED = re.compile(r"`([^`]*)`")

# What an unquoted `Opening Text` looks like by the time it fails to parse.
_ADJACENT_NAMES = re.compile(r"\b([A-Za-z_]\w*)[ \t]+([A-Za-z_]\w*)\b")

# Comparison operators the grammar admits — deliberately excludes In/NotIn/
# Is/IsNot: our dialect has no membership or identity tests.
_COMPARE_OPS: tuple[type[ast.cmpop], ...] = (
    ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE,
)

# The attribute names the dialect admits, by the position a chain holds them in:
# a method on a column, the `.str` accessor, and the methods reached through it.
# `isna`/`notna` are what `_normalize` emits for IS NULL / IS NOT NULL. Every name
# under `.str` returns a boolean mask over the rows, and a boolean Series has no
# `.str`, so no admitted chain continues past the mask it lands on.
_COLUMN_METHODS = frozenset({"isna", "notna"})
_STRING_ACCESSOR = frozenset({"str"})
_STRING_METHODS = frozenset({
    "contains", "endswith", "fullmatch", "isalnum", "isalpha", "isascii",
    "isdecimal", "isdigit", "islower", "isnumeric", "isspace", "istitle",
    "isupper", "match", "startswith",
})
_ALLOWED_ATTRIBUTES = _COLUMN_METHODS | _STRING_ACCESSOR | _STRING_METHODS

# The `.str` methods whose first argument pandas reads as a regular expression.
_REGEX_METHODS = frozenset({"contains", "match", "fullmatch"})


@dataclass(frozen=True)
class ParsedPredicate:
    columns: frozenset[str]
    pandas_expr: str
    # `pandas_expr` may hold backticks, which `ast` cannot read.
    regex_arguments: tuple[str, ...]


def parse_predicate(expr: str, columns: Collection[str] = ()) -> ParsedPredicate:
    """Without `columns` no backtick-quoted name is admitted, so the default fails closed."""
    bare_expr, quoted = _replace_backtick_quoted_names(expr, frozenset(columns))
    normalized = _normalize(bare_expr)
    try:
        tree = ast.parse(normalized, mode="eval")
    except SyntaxError as exc:
        raise PredicateError(_say_why_it_will_not_parse(expr, normalized, exc)) from exc
    _validate_node(tree.body, expr)
    read = frozenset(
        quoted.get(node.id, node.id) for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    )
    return ParsedPredicate(
        columns=read,
        pandas_expr=_restore_backtick_quoting(normalized, quoted),
        regex_arguments=tuple(find_regex_arguments(tree)),
    )


def find_regex_arguments(tree: ast.AST) -> list[str]:
    """The patterns this filter will hand a regex engine, in source order."""
    return [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _REGEX_METHODS
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]


def _replace_backtick_quoted_names(
    expr: str, columns: frozenset[str]
) -> tuple[str, dict[str, str]]:
    """A quoted name that is not a column of the caller's is refused before anything parses."""
    prefix = "_quoted"
    while prefix in expr:
        prefix += "_"
    quoted: dict[str, str] = {}

    def _take(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in columns:
            raise PredicateError(
                f"filter is not valid: {expr!r} (`{name}` is not a column here"
                + (f" — this reads {sorted(columns)})" if columns else
                   ", and this filter is read where no column list is known, so a "
                   "backtick-quoted name cannot be resolved at all)")
            )
        placeholder = f"{prefix}{len(quoted)}"
        quoted[placeholder] = name
        return placeholder

    return _BACKTICK_QUOTED.sub(_take, expr), quoted


def _restore_backtick_quoting(normalized: str, quoted: Mapping[str, str]) -> str:
    """Longest first, so `_quoted1` cannot eat the head of `_quoted11`."""
    for placeholder, name in sorted(quoted.items(), key=lambda item: -len(item[0])):
        normalized = normalized.replace(placeholder, f"`{name}`")
    return normalized


def _say_why_it_will_not_parse(expr: str, normalized: str, exc: SyntaxError) -> str:
    for match in _ADJACENT_NAMES.finditer(normalized):
        left, right = match.group(1), match.group(2)
        if keyword.iskeyword(left) or keyword.iskeyword(right):
            continue
        return (
            f"filter is not valid: {expr!r} (`{left} {right}` reads as two names — a column "
            f"whose name holds a space is written in backticks, as `{left} {right}`)"
        )
    return f"filter is not valid: {expr!r} (not parseable as a Python expression: {exc})"


def _normalize(expr: str) -> str:
    """Operands are parenthesised because bitwise `&`/`|` bind tighter than comparison."""
    e = expr
    e = e.replace(" IS NOT NULL", ".notna()")
    e = e.replace(" IS NULL", ".isna()")
    e = re.sub(r"\btrue\b", "True", e, flags=re.IGNORECASE)
    e = re.sub(r"\bfalse\b", "False", e, flags=re.IGNORECASE)
    e = re.sub(r"\bNOT\b", "not", e)

    def _split_wrap(s: str, sep: str, joiner: str) -> str:
        parts = [p.strip() for p in re.split(rf"\s+{sep}\s+", s)]
        if len(parts) <= 1:
            return s
        return f" {joiner} ".join(f"({p})" for p in parts)

    e = _split_wrap(e, "OR", "|")
    e = _split_wrap(e, "AND", "&")
    return e


def _validate_node(node: ast.AST, expr: str) -> None:
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
        _validate_attribute(node, expr)
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


def _validate_attribute(node: ast.Attribute, expr: str) -> None:
    if node.attr not in _ALLOWED_ATTRIBUTES:
        raise PredicateError(
            f"filter is not valid: {expr!r} (attribute `.{node.attr}` is not supported; a filter "
            f"may use {', '.join(sorted(_COLUMN_METHODS))} on a column, "
            f"{', '.join(sorted(_STRING_ACCESSOR))} to reach string methods, and through it "
            f"{', '.join(sorted(_STRING_METHODS))})"
        )
    _validate_node(node.value, expr)


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
