"""Tests for `app.core.predicate.parse_predicate` — the strict single-parse
predicate parser that returns both the columns a where/filter expression
references (for save-time validation) and the pandas expression to evaluate
it (for runtime execution), from one `ast` parse over a closed grammar."""
from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import PredicateError
from app.core.predicate import parse_predicate


def test_extracts_columns_and_keeps_expr_evaluable():
    p = parse_predicate("score >= 0.5")
    assert p.columns == frozenset({"score"})
    df = pd.DataFrame({"score": [0.9, 0.1]})
    assert df.eval(p.pandas_expr).tolist() == [True, False]


def test_is_null_and_boolean_ops():
    assert parse_predicate("a IS NOT NULL AND b > 0").columns == frozenset({"a", "b"})


def test_str_method_yields_only_base_column():
    assert parse_predicate("claim_id.str.startswith('x')").columns == frozenset({"claim_id"})


def test_equality_literal_is_not_a_column():
    assert parse_predicate("writer_confirmed == True").columns == frozenset({"writer_confirmed"})


def test_function_call_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("evil(x)")


def test_arithmetic_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("a + b > 0")


def test_backtick_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("`weird name` == 1")
