"""Token/cost usage of model calls.

Lives in app.core.agent rather than app.models because the SDK layer that
produces it may not import app.models — the app.core ↛ app.models contract.
"""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict


class LlmUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    # The id the backend was called with, as a string rather than an LLMModel: a run
    # record must stay readable after the deployment narrows its menu, and an enum
    # would refuse the whole manifest of a run that used a model since dropped.
    # None where nothing was called, and on every manifest written before this field.
    model: str | None = None

    def __add__(self, other: LlmUsage) -> LlmUsage:
        return LlmUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
            model=_one_model(self.model, other.model),
        )

    @classmethod
    def summed(cls, parts: Iterable[LlmUsage]) -> LlmUsage:
        total = cls()
        for part in parts:
            total = total + part
        return total


class TurnSpend(BaseModel):
    model_config = ConfigDict(frozen=True)

    # Set by hand, unlike every other `created_at` here: this is a nested model rather
    # than a PersistedModel, so nothing stamps it. The turn's end, ISO-8601.
    created_at: str
    usage: LlmUsage


def _one_model(left: str | None, right: str | None) -> str | None:
    if left and right and left != right:
        raise ValueError(
            f"cannot total usage produced by two models ({left!r} and {right!r}): the "
            "total names one model, so the other model's rows would be attributed to it"
        )
    return left or right
