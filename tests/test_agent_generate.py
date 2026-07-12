"""The headless generate-validate-retry loop (app.agent.agent.run_until_valid): it
validates an agent's JSON output INTO a caller-supplied Pydantic model, feeding the
Pydantic (or JSON) errors back to the same session until it validates or the round
budget is spent.

Driven over a SCRIPTED run_turn (canned assistant texts) so no CLI subprocess is
spawned. Coroutines are run with asyncio.run, mirroring tests/test_sdk_engine.py.
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

import pytest
from pydantic import BaseModel

from app.agent.agent import run_until_valid
from app.errors import GenerationError


class _Point(BaseModel):
    x: int
    y: int


def _scripted_run_turn(
    texts: list[str], captured_prompts: list[str]
) -> Callable[[str, str | None], Any]:
    """A run_turn that returns each canned text in turn, recording the prompt it was
    called with so a test can assert what got fed back between rounds."""
    outputs = iter(texts)

    async def run_turn(prompt: str, resume: str | None) -> tuple[str, str | None]:
        captured_prompts.append(prompt)
        return next(outputs), "sess-1"

    return run_turn


def _run(texts: list[str], *, max_rounds: int = 3) -> tuple[_Point, list[str]]:
    """Drive run_until_valid over `texts` into _Point; return (result, prompts_seen)."""
    prompts: list[str] = []
    result = asyncio.run(
        run_until_valid(
            _scripted_run_turn(texts, prompts),
            seed="SEED",
            into=_Point,
            max_rounds=max_rounds,
        )
    )
    return result, prompts


def test_returns_validated_model_on_first_valid_json() -> None:
    result, prompts = _run(['{"x": 1, "y": 2}'])
    assert result == _Point(x=1, y=2)  # a typed instance, not a dict
    assert prompts == ["SEED"]


def test_feeds_pydantic_errors_back_then_returns_corrected_model() -> None:
    result, prompts = _run(['{"x": 1}', '{"x": 1, "y": 2}'])  # first missing y
    assert result == _Point(x=1, y=2)
    assert len(prompts) == 2
    assert prompts[0] == "SEED"
    assert "y" in prompts[1]  # the missing-field error was kicked back


def test_feeds_json_error_back_when_output_is_not_json() -> None:
    result, prompts = _run(["sorry, no JSON here", '{"x": 3, "y": 4}'])
    assert result == _Point(x=3, y=4)
    assert "JSON" in prompts[1]  # the parse failure was kicked back


def test_extracts_json_from_a_fenced_block() -> None:
    result, prompts = _run(["Here you go:\n```json\n{\"x\": 5, \"y\": 6}\n```\n"])
    assert result == _Point(x=5, y=6)
    assert prompts == ["SEED"]


def test_raises_after_round_budget_never_returns_invalid() -> None:
    prompts: list[str] = []
    coro = run_until_valid(
        _scripted_run_turn(['{"x": 1}', '{"x": 2}', '{"x": 3}'], prompts),  # all missing y
        seed="SEED",
        into=_Point,
        max_rounds=3,
    )
    with pytest.raises(GenerationError) as exc_info:
        asyncio.run(coro)
    assert "_Point" in str(exc_info.value)  # names the target model
    assert len(prompts) == 3  # spent exactly the budget
