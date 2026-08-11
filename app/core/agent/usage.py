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

    def __add__(self, other: LlmUsage) -> LlmUsage:
        return LlmUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            calls=self.calls + other.calls,
        )

    @classmethod
    def summed(cls, parts: Iterable[LlmUsage]) -> LlmUsage:
        total = cls()
        for part in parts:
            total = total + part
        return total
