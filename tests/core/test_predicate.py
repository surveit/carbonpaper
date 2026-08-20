from __future__ import annotations

import pandas as pd
import pytest

from app.core.errors import PredicateError
from app.core.predicate import _ALLOWED_ATTRIBUTES, _STRING_METHODS, parse_predicate


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


def test_backtick_quoted_name_resolves_to_one_column():
    p = parse_predicate("`Opening Text` IS NULL AND `Hit Sentence` IS NOT NULL",
                        {"Opening Text", "Hit Sentence"})
    assert p.columns == frozenset({"Opening Text", "Hit Sentence"})
    df = pd.DataFrame({"Opening Text": [None, "x"], "Hit Sentence": ["a", None]})
    assert df.eval(p.pandas_expr).tolist() == [True, False]


def test_backtick_quoting_survives_the_and_split():
    """The split is textual, so a name holding ` AND ` has to be hidden before it runs."""
    p = parse_predicate("`Cats AND Dogs` > 1", {"Cats AND Dogs"})
    assert p.columns == frozenset({"Cats AND Dogs"})
    assert pd.DataFrame({"Cats AND Dogs": [0, 2]}).eval(p.pandas_expr).tolist() == [False, True]


def test_backticks_cannot_smuggle_syntax_past_the_allowlist():
    with pytest.raises(PredicateError):
        parse_predicate("`a`.__class__ == 1", {"a"})
    with pytest.raises(PredicateError):
        parse_predicate("`x` + 1 > 0", {"x"})


def test_a_quoted_name_must_be_a_column_the_caller_named():
    """The quoted span is an allowlist lookup, so pandas is never handed loose text."""
    with pytest.raises(PredicateError, match="is not a column here"):
        parse_predicate("`__import__('os').system('x')` == 1", {"a"})
    with pytest.raises(PredicateError, match="is not a column here"):
        parse_predicate("`Opening Text` IS NULL", {"Hit Sentence"})


def test_without_columns_no_quoted_name_is_admitted():
    """The default fails closed: a caller that names no columns admits no quoting."""
    with pytest.raises(PredicateError, match="is not a column here"):
        parse_predicate("`Opening Text` IS NULL")
    with pytest.raises(PredicateError):
        parse_predicate("`` == 1")


def test_uppercase_not_is_the_dialect():
    """FIND_ROWS_DESCRIPTION offers `NOT client.str.startswith(...)` as a worked example."""
    p = parse_predicate("NOT client.str.startswith('The ')")
    assert p.columns == frozenset({"client"})
    df = pd.DataFrame({"client": ["The Co", "Acme"]})
    assert df.eval(p.pandas_expr).tolist() == [False, True]


def test_unquoted_name_with_a_space_says_to_quote_it():
    with pytest.raises(PredicateError, match="written in backticks"):
        parse_predicate("Opening Text IS NULL")


def test_regex_arguments_are_found_through_backticks():
    p = parse_predicate("`Hit Sentence`.str.contains('[0-9]{4}')", {"Hit Sentence"})
    assert p.regex_arguments == ("[0-9]{4}",)


def test_unary_minus_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("-score > 0")


def test_is_comparison_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("a is b")


def test_method_call_with_keyword_argument_rejected():
    with pytest.raises(PredicateError):
        parse_predicate("claim_id.str.contains('x', regex=True)")


# ── the attribute allowlist ──────────────────────────────────────────────────
# pandas resolves an attribute chain with a real getattr and calls the result
# inside its expression PARSER, before any engine runs, so the validator is the
# only thing standing between an authored predicate and arbitrary execution.
_ESCAPES = [
    "n.__class__.__init__.__globals__.get('warnings').warn('PWNED')",
    "name.__class__",
    "name.__init__",
    "name.__class__.__globals__",
    "name.__class__.__init__.__globals__",
    "name.__class__.mro()",
    "name.__reduce__()",
]


@pytest.mark.parametrize("expr", _ESCAPES)
def test_dunder_attribute_walk_rejected(expr):
    with pytest.raises(PredicateError, match="attribute"):
        parse_predicate(expr)


def test_unknown_attribute_rejected():
    with pytest.raises(PredicateError, match="attribute"):
        parse_predicate("score.values")


def test_str_accessor_method_not_in_dialect_rejected():
    with pytest.raises(PredicateError, match="attribute"):
        parse_predicate("claim_id.str.zfill(3)")


def test_rejection_message_names_every_allowed_attribute():
    with pytest.raises(PredicateError) as raised:
        parse_predicate("score.values")
    assert all(name in str(raised.value) for name in _ALLOWED_ATTRIBUTES)


# The one argument each `.str` method needs, and the ones that take none. A method in
# neither has no call form here, and `_render_string_method_call` fails rather than
# leaving it unexercised.
_STRING_METHOD_ARGUMENTS = {
    "contains": "'b'",
    "endswith": "'1'",
    "fullmatch": "'A.*'",
    "match": "'A'",
    "startswith": "'A'",
}
_NO_ARGUMENT_STRING_METHODS = frozenset({
    "isalnum", "isalpha", "isascii", "isdecimal", "isdigit",
    "islower", "isnumeric", "isspace", "istitle", "isupper",
})


def _render_string_method_call(method: str) -> str:
    if method in _STRING_METHOD_ARGUMENTS:
        return f"claim_id.str.{method}({_STRING_METHOD_ARGUMENTS[method]})"
    if method in _NO_ARGUMENT_STRING_METHODS:
        return f"claim_id.str.{method}()"
    raise AssertionError(
        f"`.str.{method}` is allowlisted but this test declares no call form for it — "
        "add one to _STRING_METHOD_ARGUMENTS or _NO_ARGUMENT_STRING_METHODS"
    )


@pytest.mark.parametrize("expr", [
    "a IS NULL",
    "a IS NOT NULL",
    *(_render_string_method_call(m) for m in sorted(_STRING_METHODS)),
])
def test_dialect_attributes_still_accepted(expr):
    assert parse_predicate(expr).columns


@pytest.mark.parametrize("method", sorted(_STRING_METHODS))
def test_allowlisted_string_method_yields_a_boolean_row_mask(method):
    df = pd.DataFrame({"claim_id": ["Abc1", "xyz", " ", "42"]})
    parsed = parse_predicate(_render_string_method_call(method))
    mask = df.eval(parsed.pandas_expr)
    assert isinstance(mask, pd.Series)
    assert mask.dtype == bool
    assert len(mask) == len(df)
    assert len(df.query(parsed.pandas_expr)) == int(mask.sum())
