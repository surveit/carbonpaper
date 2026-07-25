"""Spike (issue #194): a `where` predicate parsed ONCE, by DuckDB's own parser.

`app.core.predicate` (the PR #182 interim fix) satisfies the invariant —
"the parse used to validate a filter's columns must be the same parse that
executes it" — by *approximation*: it parses with `ast`, executes with
`pandas.eval`, and closes the gap by refusing every construct where the two
could disagree. The grammar has to stay narrow precisely because the two
parsers are different programs.

DuckDB gives the invariant literally, via `json_serialize_sql` /
`json_deserialize_sql`:

    expr ──parse──▶ AST ──┬──▶ COLUMN_REF nodes   (what validation reads)
                          └──▶ rendered SQL       (what execution runs)

The executed SQL is *rendered back from the very tree validation inspected*, so
"validation accepts it iff execution can run it" holds by construction rather
than by a hand-maintained node allowlist. `parsed.sql` is deliberately the
rendered form, not the author's original string — using the original would
re-open the two-parses gap through the back door.

Two findings this module encodes, both of which surprised the spike:

- **Parsing is not binding.** DuckDB's parser accepts `amount >> ` (`>>` is its
  JSON-extract operator in postfix position) and any `f(x)` for unknown `f`;
  those fail later, at bind time. Column-reference checking alone is therefore
  *not* the whole save-time check — `bind_predicate_against_schema` runs the
  real binder against a zero-row typed relation to catch the rest, which is
  strictly more than the pandas path can check without executing on real data.
- **A subquery breaks the column check, not the parse.** Inside a subquery,
  names resolve against a different relation, so the COLUMN_REF nodes there are
  not columns of *this* stage's input. They are rejected rather than
  mis-reported, which is the one place this dialect still has to say "no".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

import duckdb
import pyarrow as pa

from app.core.errors import PredicateError
from app.models import TableSchema

from .arrow_types import arrow_schema_for

# The statement a bare predicate is embedded in so DuckDB will parse it.
# `SELECT 1 WHERE <expr>` is the smallest statement whose only free expression
# is the predicate, so the resulting tree holds the predicate and nothing else.
_PROBE_PREFIX = "SELECT 1 WHERE "

# Node classes in DuckDB's serialized parse tree that this module inspects by
# name. `class` is the discriminator on every expression node.
_NODE_CLASS_KEY = "class"
_COLUMN_REF_CLASS = "COLUMN_REF"
_SUBQUERY_CLASS = "SUBQUERY"

# The relation name a bound predicate is checked against. Only ever used for a
# zero-row probe relation, never for real data.
_PROBE_RELATION = "predicate_probe"


@dataclass(frozen=True)
class ParsedSqlPredicate:
    """One `where` expression, parsed once by DuckDB.

    `columns` is every column the tree references — the save-time check against
    a declared schema. `sql` is the same tree rendered back to SQL — what
    execution runs. Same parse, two uses.
    """

    columns: frozenset[str]
    sql: str


def parse_sql_predicate(
    expr: str, *, connection: "duckdb.DuckDBPyConnection | None" = None
) -> ParsedSqlPredicate:
    """Parse `expr` with DuckDB and return its column references alongside the
    SQL rendered back from that same parse.

    Raises `PredicateError` — the same error type `app.core.predicate` raises,
    so the substrate swap does not change the save-time error contract — when
    the expression does not parse, is not a single statement, carries no
    predicate at all, or contains a subquery.
    """
    con = connection if connection is not None else duckdb.connect()
    serialized = _serialize_or_raise(con, _PROBE_PREFIX + expr, expr)
    where_clause = _where_clause_of(serialized)
    _reject_subqueries(where_clause, expr)
    rendered = _render_or_raise(con, serialized, expr)
    return ParsedSqlPredicate(
        columns=frozenset(_column_names(where_clause)),
        sql=rendered,
    )


def bind_predicate_against_schema(
    expr: str, schema: TableSchema, *, connection: "duckdb.DuckDBPyConnection | None" = None
) -> ParsedSqlPredicate:
    """Parse `expr`, then *bind* it against a zero-row relation carrying
    `schema`'s Arrow types — the full save-time check.

    Binding is what turns "these column names exist" into "this predicate can
    actually run against this schema": an unknown column, an unknown function,
    or an operator that does not apply to a column's type all raise here,
    with DuckDB's own message. It costs one zero-row query and no data.
    """
    con = connection if connection is not None else duckdb.connect()
    parsed = parse_sql_predicate(expr, connection=con)
    empty = pa.Table.from_pylist([], schema=arrow_schema_for(schema))
    con.register(_PROBE_RELATION, empty)
    try:
        con.execute(f"SELECT 1 FROM {_PROBE_RELATION} WHERE {parsed.sql}").fetchall()
    except duckdb.Error as exc:
        raise PredicateError(f"filter is not valid: {expr!r} ({exc})") from exc
    finally:
        con.unregister(_PROBE_RELATION)
    return parsed


def _serialize_or_raise(con: "duckdb.DuckDBPyConnection", statement: str, expr: str) -> str:
    """DuckDB's serialized parse of `statement`, or `PredicateError`.

    `json_serialize_sql` reports a parse failure in its own JSON payload rather
    than raising, and reports a multi-statement input as an error too ("only
    SELECT statements can be serialized") — which is what makes `a = 1; DROP
    TABLE t` a rejection rather than a second statement smuggled through.
    """
    try:
        row = con.execute("SELECT json_serialize_sql(?)", [statement]).fetchone()
    except duckdb.Error as exc:  # pragma: no cover - serialize reports in-band
        raise PredicateError(f"filter is not valid: {expr!r} ({exc})") from exc
    if row is None:
        raise PredicateError(f"filter is not valid: {expr!r} (no parse returned)")
    payload = json.loads(row[0])
    if payload["error"]:
        raise PredicateError(
            f"filter is not valid: {expr!r} ({payload['error_message']})"
        )
    if len(payload["statements"]) != 1:
        raise PredicateError(
            f"filter is not valid: {expr!r} (expected one expression, got "
            f"{len(payload['statements'])} statements)"
        )
    return row[0]


def _where_clause_of(serialized: str) -> Any:
    """The parsed WHERE subtree of the probe statement.

    No None guard: the probe statement is `SELECT 1 WHERE <expr>`, so a parse
    that succeeded at all has a WHERE clause. Were that ever untrue, the tree
    would render back as a bare `SELECT 1` and `_render_or_raise`'s prefix
    check would refuse it — the guard exists once, downstream, not twice.
    """
    # A genuine dynamic-JSON boundary: DuckDB's parse tree is a foreign,
    # node-class-tagged document whose shape varies per node type, so it is
    # walked as JSON rather than modelled.
    payload: dict[str, Any] = json.loads(serialized)
    return payload["statements"][0]["node"]["where_clause"]


def _render_or_raise(con: "duckdb.DuckDBPyConnection", serialized: str, expr: str) -> str:
    """The predicate rendered back out of `serialized` — the SQL execution
    runs. Deserializing the whole probe statement and stripping the known
    prefix keeps the rendered text tied to the parse: nothing about `expr`'s
    original spelling survives into what runs."""
    row = con.execute("SELECT json_deserialize_sql(?)", [serialized]).fetchone()
    if row is None or not row[0].startswith(_PROBE_PREFIX):
        raise PredicateError(
            f"filter is not valid: {expr!r} (parse did not render back to a predicate)"
        )
    return row[0][len(_PROBE_PREFIX):]


def _reject_subqueries(node: Any, expr: str) -> None:
    """Refuse a predicate containing a subquery: its inner COLUMN_REF nodes
    resolve against a different relation, so treating them as this stage's
    input columns would make the validate/execute correspondence a lie."""
    for candidate in _walk(node):
        if candidate.get(_NODE_CLASS_KEY) == _SUBQUERY_CLASS:
            raise PredicateError(
                f"filter is not valid: {expr!r} (a subquery is not supported — its "
                "columns resolve against another relation, not this stage's input)"
            )


def _column_names(node: Any) -> Iterator[str]:
    """Every column a parse tree references. A qualified reference serializes
    as `["alias", "column"]`, so the last element is the column name."""
    for candidate in _walk(node):
        if candidate.get(_NODE_CLASS_KEY) == _COLUMN_REF_CLASS:
            names = candidate["column_names"]
            if names:
                yield str(names[-1])


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every dict node in a parse tree, depth-first."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)
