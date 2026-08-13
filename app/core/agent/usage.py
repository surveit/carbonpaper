"""Token/cost usage of model calls.

Lives in app.core.agent rather than app.models because the SDK layer that
produces it may not import app.models — the app.core ↛ app.models contract.
"""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict

from app.core.llm.options import LLMModel


class LlmUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    # Which model the backend was called with. None reads as `not recorded`: nothing
    # was called, or the manifest predates the field. A deployment that DROPS a member
    # of the menu therefore stops being able to read the manifest of a run that used
    # it — the day that trade stops being worth it, this field gets its own wider type
    # rather than every stage config losing the enum.
    model: LLMModel | None = None

    def __add__(self, other: LlmUsage) -> LlmUsage:
        return LlmUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
            model=_assert_one_model(self.model, other.model),
        )

    @classmethod
    def summed(cls, parts: Iterable[LlmUsage]) -> LlmUsage:
        total = cls()
        for part in parts:
            total = total + part
        return total


def _assert_one_model(left: LLMModel | None, right: LLMModel | None) -> LLMModel | None:
    if left and right and left != right:
        raise ValueError(
            f"cannot total usage produced by two models ({left!r} and {right!r}): the "
            "total names one model, so the other model's rows would be attributed to it"
        )
    return left or right
