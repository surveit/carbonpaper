"""A timed-out call is the one failure a re-ask cannot help.

Every attempt gets its own full timeout, so retrying a timeout multiplies the
wait by `max_retries + 1` without changing the odds. A rate limit takes exactly
this path, because nothing here classifies one.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from app.models.stages.llm_transform import LLMConfig
from app.runtime import llm as runtime_llm

_TIMEOUT_S = 0.4


class _Reply(BaseModel):
    label: str


@pytest.fixture(autouse=True)
def offline_agent(monkeypatch):
    monkeypatch.setattr(runtime_llm, "require_agent_backend", lambda: None)
    monkeypatch.setattr(runtime_llm, "DEFAULT_TIMEOUT_S", _TIMEOUT_S)


def _call(max_retries: int = 3) -> tuple[float, BaseException | None]:
    config = LLMConfig(prompt_data_template="{text}", max_retries=max_retries)
    started = time.perf_counter()
    try:
        runtime_llm.call_llm("score", config, {"text": "t"}, reply_model=_Reply)
        raised: BaseException | None = None
    except BaseException as exc:  # noqa: BLE001 — the test is about WHICH one arrives
        raised = exc
    return time.perf_counter() - started, raised


def test_a_timed_out_call_is_not_re_asked(monkeypatch):
    runs = {"n": 0}

    async def hang(self, emit=None):
        runs["n"] += 1
        await asyncio.sleep(_TIMEOUT_S * 20)

    monkeypatch.setattr("app.core.agent.agent.Agent.run", hang)
    elapsed, raised = _call()

    assert runs["n"] == 1                        # one timeout, not four
    assert isinstance(raised, asyncio.TimeoutError)
    assert elapsed < _TIMEOUT_S * 3


def test_a_fast_failure_still_gets_every_retry(monkeypatch):
    """Not a retry cap: what fails instantly is exactly what a re-ask can help."""
    runs = {"n": 0}

    async def refuse(self, emit=None):
        runs["n"] += 1
        raise RuntimeError("429 rate_limit_error")

    monkeypatch.setattr("app.core.agent.agent.Agent.run", refuse)
    monkeypatch.setattr("app.runtime.llm.time.sleep", lambda s: None)
    elapsed, raised = _call(max_retries=3)

    assert runs["n"] == 4
    assert str(raised) == "429 rate_limit_error"
