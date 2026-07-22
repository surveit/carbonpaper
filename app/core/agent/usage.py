"""Token/cost usage of model calls — a structured value, not a bare dict.

Produced from a CLI turn's ResultMessage (app.core.agent.sdk_engine), summed
across a row's retry attempts and a stage's rows (app.runtime), and dumped to a
plain dict only at the JSON manifest boundary (app.runtime.runner). Lives in
app.core.agent (not app.models) because the SDK layer that produces it may not
import app.models — the app.core ↛ app.models contract.
"""
from __future__ import annotations

from collections.abc import Iterable

from pydantic import BaseModel, ConfigDict


class LlmUsage(BaseModel):
    """One-or-more model calls' token counts and estimated cost. Frozen so a
    computed total can't be mutated after the fact; fold parts with `+` or
    `summed`. The default instance is the zero/identity."""

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
        """Field-wise total of `parts` (empty -> the zero instance)."""
        total = cls()
        for part in parts:
            total = total + part
        return total
