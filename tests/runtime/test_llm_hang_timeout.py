"""Whether the per-call timeout can actually end a stuck call.

`_run_agent` wraps the agent in `asyncio.wait_for`, which cancels at an AWAIT
point. These two tests run the same 0.2s budget against a call that awaits and a
call that does not, and time what happens.
"""
from __future__ import annotations

import asyncio
import time

import pytest
from pydantic import BaseModel

from app.runtime import llm as runtime_llm
from app.models.stages.llm_transform import LLMConfig

_BLOCK_S = 2.0
_TIMEOUT_S = 0.2


class _Reply(BaseModel):
    label: str


def _call(monkeypatch, hang) -> tuple[float, BaseException | None]:
    monkeypatch.setattr(runtime_llm, "require_agent_backend", lambda: None)
    monkeypatch.setattr(runtime_llm, "DEFAULT_TIMEOUT_S", _TIMEOUT_S)
    monkeypatch.setattr("app.core.agent.agent.Agent.run", hang)
    config = LLMConfig(prompt_data_template="{text}", max_retries=0)
    started = time.perf_counter()
    try:
        runtime_llm.call_llm("score", config, {"text": "t"}, reply_model=_Reply)
        raised: BaseException | None = None
    except BaseException as exc:  # noqa: BLE001 — the test is about WHICH one arrives
        raised = exc
    return time.perf_counter() - started, raised


def test_timeout_ends_a_call_that_awaits(monkeypatch):
    async def hang(self, emit=None):
        await asyncio.sleep(_BLOCK_S)

    elapsed, raised = _call(monkeypatch, hang)

    assert isinstance(raised, asyncio.TimeoutError)
    assert elapsed < _BLOCK_S


def test_timeout_cannot_end_a_call_that_blocks_the_loop(monkeypatch):
    async def hang(self, emit=None):
        time.sleep(_BLOCK_S)          # a sync read, an un-awaited subprocess wait
        return _Reply(label="late")

    elapsed, raised = _call(monkeypatch, hang)

    assert raised is None             # the answer came back, long past its budget
    assert elapsed >= _BLOCK_S        # the 0.2s timeout never fired
