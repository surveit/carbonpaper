"""One unit of work has ONE time budget, however many times it retries inside.

Before this, every attempt got a fresh `DEFAULT_TIMEOUT_S`, so a row that kept
timing out occupied `max_retries + 1` full timeouts and a batched chunk squared
that. These pin the budget as the bound, and the two ways it can be spent.
"""
from __future__ import annotations

import asyncio
import time

import pandas as pd
import pytest
from pydantic import BaseModel

from app.models import parse_stage
from app.models.stage import StageType
from app.runtime.stages import HANDLERS
from app.runtime.stages import llm_transform as lt
from conftest import as_inputs, contribution_of, make_run_context, place_stage

from app.core.errors import LLMError
from app.models.stages.llm_transform import LLMConfig
from app.runtime import llm as runtime_llm
from app.runtime.llm import Deadline

_BUDGET_S = 0.4


class _Reply(BaseModel):
    label: str


@pytest.fixture(autouse=True)
def offline_agent(monkeypatch):
    monkeypatch.setattr(runtime_llm, "require_agent_backend", lambda: None)
    monkeypatch.setattr(runtime_llm, "DEFAULT_TIMEOUT_S", _BUDGET_S)
    # The named seam, NOT `time.sleep` — `app.runtime.llm.time` IS the time
    # module, so patching through it stops this test's own clock too.
    monkeypatch.setattr(runtime_llm, "_sleep_before_retry", lambda attempt, deadline: None)


def _call(max_retries: int = 3) -> tuple[float, BaseException | None]:
    config = LLMConfig(prompt_data_template="{text}", max_retries=max_retries)
    started = time.perf_counter()
    try:
        runtime_llm.call_llm("score", config, {"text": "t"}, reply_model=_Reply)
        raised: BaseException | None = None
    except BaseException as exc:  # noqa: BLE001 — the test is about WHICH one arrives
        raised = exc
    return time.perf_counter() - started, raised


def test_a_slow_failure_spends_the_whole_budget_and_does_not_retry(monkeypatch):
    """The timing-out call IS the budget — retrying it could only time out again."""
    runs = {"n": 0}

    async def hang(self, emit=None):
        runs["n"] += 1
        await asyncio.sleep(_BUDGET_S * 10)

    monkeypatch.setattr("app.core.agent.agent.Agent.run", hang)
    elapsed, raised = _call()

    assert runs["n"] == 1                       # not 4
    assert isinstance(raised, asyncio.TimeoutError)
    assert elapsed < _BUDGET_S * 3              # one budget, not four


def test_fast_failures_still_get_every_retry(monkeypatch):
    """A budget is not a retry cap: what fails instantly leaves the budget for a re-try."""
    runs = {"n": 0}

    async def refuse(self, emit=None):
        runs["n"] += 1
        raise RuntimeError("429 rate_limit_error")

    monkeypatch.setattr("app.core.agent.agent.Agent.run", refuse)
    elapsed, raised = _call(max_retries=3)

    assert runs["n"] == 4                       # the retries still happen
    assert str(raised) == "429 rate_limit_error"
    assert elapsed < _BUDGET_S


def test_a_spent_budget_raises_rather_than_asking(monkeypatch):
    runs = {"n": 0}

    async def never_reached(self, emit=None):
        runs["n"] += 1

    monkeypatch.setattr("app.core.agent.agent.Agent.run", never_reached)
    with pytest.raises(LLMError, match="already spent"):
        runtime_llm._run_agent(
            "sys", "task", _Reply, "claude-haiku-4-5-20251001", 3, None, Deadline(-1.0))
    assert runs["n"] == 0


def test_a_researching_row_gets_the_research_budget():
    assert runtime_llm.open_row_deadline(
        LLMConfig(prompt_data_template="{t}", tools=["WebSearch"])
    ).budget_s == runtime_llm.RESEARCH_TIMEOUT_S
    assert runtime_llm.open_row_deadline(
        LLMConfig(prompt_data_template="{t}")
    ).budget_s == runtime_llm.DEFAULT_TIMEOUT_S


def test_a_chunks_re_asks_share_one_budget(monkeypatch):
    """The batched loop squared the budget before: 4 re-asks x 4 backend retries, each a full timeout."""
    stage = parse_stage({
        "id": "score", "description": "Score", "type": "llm_transform",
        "inputs": [{"id": "load"}],
        "signature": {"form": "extends",
                      "reads": [{"input": "load", "columns": [
                          {"name": "text", "type": "str", "nullable": True}]}],
                      "adds": [{"name": "label", "type": "str", "nullable": True}]},
        "llm": {"prompt_data_template": "score {text}", "batch_size": 2, "max_retries": 3}})

    calls = {"n": 0}

    def confused_and_slow(*a, **k):
        calls["n"] += 1
        time.sleep(_BUDGET_S / 2)
        return {"results": [{"row_number": 0, "label": "L0"}]}   # never rejoins

    monkeypatch.setattr(lt, "call_llm_batch", confused_and_slow)
    out = HANDLERS[StageType.llm_transform].execute(
        place_stage(stage, load={"columns": [
            {"name": "text", "type": "str", "nullable": True}]}),
        as_inputs({"load": pd.DataFrame({"text": ["a", "b"]})}), make_run_context())

    assert calls["n"] < 4                                  # the budget cut the re-asks short
    assert "budget ran out" in contribution_of(out).row_errors[0]["message"]
