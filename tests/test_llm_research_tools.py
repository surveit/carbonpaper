"""An llm_transform stage may be granted read-only research tools.

Covers the boundary (which tools are grantable, and why batching is refused with
them) and the plumbing (tools reach the agent, and a research row gets the research
turn/timeout budget rather than the 180s per-row one).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.core.agent.agent import SUBMIT_ANSWER_TOOL, Agent
from app.models.stages.llm_transform import GRANTABLE_TOOLS, LLMConfig
from app.runtime import llm as runtime_llm
from app.runtime.options import (
    DEFAULT_TIMEOUT_S,
    RESEARCH_MAX_TURNS,
    RESEARCH_TIMEOUT_S,
)


class Reply(BaseModel):
    answer: str


def _config(**kw) -> LLMConfig:
    return LLMConfig(prompt_data_template="{q}", **kw)


# ── what is grantable ────────────────────────────────────────────────────────
def test_research_tools_are_accepted():
    cfg = _config(tools=["WebSearch", "WebFetch"])
    assert cfg.tools == ["WebSearch", "WebFetch"]


def test_bash_is_grantable():
    assert _config(tools=["Bash"]).tools == ["Bash"]


@pytest.mark.parametrize("tool", ["websearch", "web_search", "Websearch", "Fetch"])
def test_unknown_tool_names_are_refused(tool):
    with pytest.raises(ValidationError) as err:
        _config(tools=[tool])
    assert "unknown tool name" in str(err.value)


def test_tools_refuse_batching():
    """Batch-mates share one context, so one row's research would leak into another's answer."""
    with pytest.raises(ValidationError) as err:
        _config(tools=["WebSearch"], batch_size=4)
    assert "batch_size=1" in str(err.value)


def test_batching_still_allowed_without_tools():
    assert _config(batch_size=8).batch_size == 8


def test_grantable_set_covers_search_fetch_and_extraction():
    assert {"WebSearch", "WebFetch", "Bash"} <= GRANTABLE_TOOLS


# ── plumbing: tools reach the agent ──────────────────────────────────────────
def test_agent_grants_extra_tools_alongside_submit_answer():
    agent = Agent(
        system_prompt="s", target_schema=Reply, task="t",
        extra_tools=["WebSearch"], max_turns=RESEARCH_MAX_TURNS,
    )
    engine = agent.build_engine()
    assert any(SUBMIT_ANSWER_TOOL in t for t in engine._allowed_tools)
    assert "WebSearch" in engine._allowed_tools
    assert engine._max_turns == RESEARCH_MAX_TURNS


def test_agent_without_extra_tools_is_submit_only():
    engine = Agent(system_prompt="s", target_schema=Reply, task="t").build_engine()
    assert "WebSearch" not in engine._allowed_tools


# ── plumbing: the research budget ────────────────────────────────────────────
def _capture(monkeypatch):
    seen: dict = {}

    class StubAgent:
        def __init__(self, **kw):
            seen.update(kw)
            self._last_usage = None

        async def run(self, emit=None):
            return Reply(answer="ok")

    monkeypatch.setattr(runtime_llm, "Agent", StubAgent)
    monkeypatch.setattr(runtime_llm, "require_agent_backend", lambda: None)

    async def fake_wait_for(aw, timeout):
        seen["timeout"] = timeout
        return await aw

    monkeypatch.setattr(runtime_llm.asyncio, "wait_for", fake_wait_for)
    return seen


def test_research_row_gets_research_budget(monkeypatch):
    seen = _capture(monkeypatch)
    runtime_llm.call_llm(
        "s1", _config(tools=["WebSearch"]), {"q": "who owns this mill?"},
        reply_model=Reply,
    )
    assert seen["extra_tools"] == ["WebSearch"]
    assert seen["max_turns"] == RESEARCH_MAX_TURNS
    assert seen["timeout"] == RESEARCH_TIMEOUT_S


def test_plain_row_keeps_the_cheap_budget(monkeypatch):
    seen = _capture(monkeypatch)
    runtime_llm.call_llm("s1", _config(), {"q": "2+2?"}, reply_model=Reply)
    assert seen["extra_tools"] == []
    assert seen["max_turns"] is None
    assert seen["timeout"] == DEFAULT_TIMEOUT_S
