"""What one model request costs in US dollars, from the token counts its reply reports."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, NonNegativeInt

_TOKENS_PER_MTOK = 1_000_000


class RateCard(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_usd_per_mtok: float
    output_usd_per_mtok: float


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True)

    short_prompt_max_tokens: int
    short_prompt: RateCard
    long_prompt: RateCard
    # Multiples of the input rate on whichever card the prompt's length chose.
    cache_read_multiplier: float
    cache_write_5m_multiplier: float


class RequestTokens(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: NonNegativeInt
    # A 5-minute cache write; a 1-hour write bills at another multiplier and has no field here.
    cache_write_5m_input_tokens: NonNegativeInt
    cache_read_input_tokens: NonNegativeInt
    output_tokens: NonNegativeInt


# https://platform.claude.com/docs/en/about-claude/pricing
_PRICES: dict[str, ModelPrice] = {
    "claude-haiku-5-5": ModelPrice(
        short_prompt_max_tokens=100_000,
        short_prompt=RateCard(input_usd_per_mtok=0.10, output_usd_per_mtok=0.50),
        long_prompt=RateCard(input_usd_per_mtok=0.50, output_usd_per_mtok=2.50),
        cache_read_multiplier=0.1,
        cache_write_5m_multiplier=1.25,
    ),
}


def price_request(model: str, tokens: RequestTokens) -> float:
    price = _find_price(model)
    card = _choose_rate_card(price, tokens)
    tokens_at_input_rate = (
        tokens.input_tokens
        + tokens.cache_write_5m_input_tokens * price.cache_write_5m_multiplier
        + tokens.cache_read_input_tokens * price.cache_read_multiplier
    )
    input_usd = tokens_at_input_rate * card.input_usd_per_mtok / _TOKENS_PER_MTOK
    output_usd = tokens.output_tokens * card.output_usd_per_mtok / _TOKENS_PER_MTOK
    return input_usd + output_usd


def validate_priced(model: str) -> None:
    _find_price(model)


def _find_price(model: str) -> ModelPrice:
    if model not in _PRICES:
        raise ValueError(
            f"{model} has no price, so what a call to it costs cannot be reported; add its "
            "rates to _PRICES in app/core/agent/prices.py"
        )
    return _PRICES[model]


def _choose_rate_card(price: ModelPrice, tokens: RequestTokens) -> RateCard:
    prompt_tokens = (
        tokens.input_tokens + tokens.cache_write_5m_input_tokens + tokens.cache_read_input_tokens
    )
    if prompt_tokens <= price.short_prompt_max_tokens:
        return price.short_prompt
    return price.long_prompt
