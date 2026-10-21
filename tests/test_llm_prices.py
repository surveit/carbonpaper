from __future__ import annotations

import pytest

from app.core.llm import LLMModel
from app.core.llm.prices import RequestTokens, price_request, validate_priced

_HAIKU = LLMModel.claude_haiku_5_5


def _tokens(*, uncached: int = 0, written: int = 0, read: int = 0, output: int = 0) -> RequestTokens:
    return RequestTokens(
        input_tokens=uncached,
        cache_write_5m_input_tokens=written,
        cache_read_input_tokens=read,
        output_tokens=output,
    )


def test_a_short_prompt_bills_at_the_short_prompt_rates() -> None:
    cost = price_request(_HAIKU, _tokens(uncached=1_000, output=500))

    assert cost == pytest.approx((1_000 * 0.10 + 500 * 0.50) / 1_000_000)


def test_a_long_prompt_bills_input_and_output_at_the_long_prompt_rates() -> None:
    cost = price_request(_HAIKU, _tokens(uncached=200_000, output=1_000))

    assert cost == pytest.approx((200_000 * 0.50 + 1_000 * 2.50) / 1_000_000)


def test_a_prompt_of_exactly_one_hundred_thousand_tokens_is_still_short() -> None:
    at_the_line = price_request(_HAIKU, _tokens(uncached=100_000))
    over_it = price_request(_HAIKU, _tokens(uncached=100_001))

    assert at_the_line == pytest.approx(100_000 * 0.10 / 1_000_000)
    assert over_it == pytest.approx(100_001 * 0.50 / 1_000_000)


def test_cached_tokens_count_toward_the_prompt_length_that_picks_the_card() -> None:
    cost = price_request(_HAIKU, _tokens(uncached=1, read=100_000))

    assert cost == pytest.approx((1 * 0.50 + 100_000 * 0.50 * 0.1) / 1_000_000)


def test_a_cache_read_costs_a_tenth_and_a_cache_write_a_quarter_more_than_input() -> None:
    read = price_request(_HAIKU, _tokens(read=10_000))
    written = price_request(_HAIKU, _tokens(written=10_000))

    assert read == pytest.approx(10_000 * 0.10 * 0.1 / 1_000_000)
    assert written == pytest.approx(10_000 * 0.10 * 1.25 / 1_000_000)


def test_a_model_with_no_price_refuses_rather_than_costing_nothing() -> None:
    with pytest.raises(ValueError, match="claude-opus-5 has no price.*add its rates"):
        price_request(LLMModel.claude_opus_5, _tokens(uncached=1))


def test_validating_an_unpriced_model_raises_the_same_error_before_any_call() -> None:
    validate_priced(_HAIKU)
    with pytest.raises(ValueError, match="claude-opus-5 has no price"):
        validate_priced(LLMModel.claude_opus_5)
