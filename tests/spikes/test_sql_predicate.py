"""Spike (issue #194): the invariant — one parse, two uses — over DuckDB.

`app.core.predicate` satisfies the invariant by refusing everything two
different parsers could read differently. These tests check that the SQL
substrate satisfies it *structurally* instead: what executes is rendered back
out of the tree that was validated, so the two cannot describe different
expressions. They also pin the two places the invariant still needs help — a
subquery (names resolve elsewhere) and the parse/bind distinction.
"""
from __future__ import annotations

import duckdb
import pytest

from app.core.errors import PredicateError
from app.models import TableSchema
from app.spikes.substrate.sql_predicate import (
    bind_predicate_against_schema,
    parse_sql_predicate,
)


def _schema() -> TableSchema:
    return TableSchema.model_validate({
        "columns": [
            {"name": "amount", "type": "float"},
            {"name": "country", "type": "str"},
            {"name": "name", "type": "str"},
        ]
    })


def test_column_references_come_from_the_parse():
    parsed = parse_sql_predicate("amount > 1000 AND country = 'FR'")
    assert parsed.columns == {"amount", "country"}


def test_executed_sql_is_rendered_back_from_the_validated_parse():
    """The invariant, literally: `.sql` is DuckDB's own rendering of the tree
    `.columns` was read from — not the author's original string re-parsed."""
    parsed = parse_sql_predicate("amount>1000 and country='FR'")
    assert parsed.sql == "((amount > 1000) AND (country = 'FR'))"
    reparsed = parse_sql_predicate(parsed.sql)
    assert reparsed.columns == parsed.columns
    assert reparsed.sql == parsed.sql


def test_rendered_sql_actually_runs_against_the_columns_it_reported():
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE t AS SELECT * FROM (VALUES (2000.0, 'FR'), (5.0, 'IE')) AS v(amount, country)"
    )
    parsed = parse_sql_predicate("amount > 1000 AND country = 'FR'")
    rows = con.execute(f"SELECT country FROM t WHERE {parsed.sql}").fetchall()
    assert rows == [("FR",)]


def test_null_comparison_is_sql_null_not_a_nan_surprise():
    """`amount > 1000` over a NULL amount is UNKNOWN → the row is not selected,
    and `IS NULL` is the only thing that finds it. No `float('nan')` anywhere."""
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (2000.0), (NULL)) AS v(amount)")
    kept = con.execute(f"SELECT count(*) FROM t WHERE {parse_sql_predicate('amount > 1000').sql}")
    assert kept.fetchone() == (1,)
    nulls = con.execute(f"SELECT count(*) FROM t WHERE {parse_sql_predicate('amount IS NULL').sql}")
    assert nulls.fetchone() == (1,)


def test_is_not_null_needs_no_dialect_translation():
    """`app.core.predicate` string-replaces ` IS NOT NULL` with `.notna()`
    before pandas can read it. SQL is already the dialect."""
    parsed = parse_sql_predicate("name IS NOT NULL")
    assert parsed.columns == {"name"}
    assert parsed.sql == '("name" IS NOT NULL)'


def test_qualified_reference_reports_the_column_name():
    assert parse_sql_predicate("t.amount > 1").columns == {"amount"}


def test_unparseable_expression_is_refused():
    with pytest.raises(PredicateError, match="filter is not valid"):
        parse_sql_predicate("amount > ")


def test_second_statement_cannot_be_smuggled_in():
    with pytest.raises(PredicateError, match="filter is not valid"):
        parse_sql_predicate("1=1; DROP TABLE t")


def test_subquery_is_refused_because_its_columns_resolve_elsewhere():
    with pytest.raises(PredicateError, match="subquery"):
        parse_sql_predicate("amount IN (SELECT x FROM other)")


def test_empty_expression_is_refused():
    with pytest.raises(PredicateError, match="filter is not valid"):
        parse_sql_predicate("")


def test_binding_catches_an_unknown_column_the_parse_alone_would_accept():
    """Column-reference extraction says `nope` is referenced; only the binder
    says it does not exist — and it is the same binder execution uses."""
    assert parse_sql_predicate("nope > 1").columns == {"nope"}
    with pytest.raises(PredicateError, match="nope"):
        bind_predicate_against_schema("nope > 1", _schema())


def test_binding_catches_an_unknown_function_that_parses_fine():
    """The spike's parse/bind finding: DuckDB's parser accepts any `f(x)`;
    only binding rejects one that does not exist."""
    assert parse_sql_predicate("no_such_fn(amount) > 1").columns == {"amount"}
    with pytest.raises(PredicateError):
        bind_predicate_against_schema("no_such_fn(amount) > 1", _schema())


def test_binding_accepts_a_predicate_the_schema_supports():
    parsed = bind_predicate_against_schema("amount > 1000 AND name IS NOT NULL", _schema())
    assert parsed.columns == {"amount", "name"}


def test_binding_costs_no_rows():
    """The bind probe is a zero-row relation, so save-time validation never
    reads data — the property that lets it run on every save."""
    parsed = bind_predicate_against_schema("upper(country) = 'FR'", _schema())
    assert parsed.columns == {"country"}
