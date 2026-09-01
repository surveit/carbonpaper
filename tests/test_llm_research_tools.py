"""An llm_transform stage may be granted read-only research tools.

Covers the boundary (which tools are grantable, and why batching is refused with
them) and the plumbing (tools reach the agent, and a research row gets the research
turn/timeout budget rather than the 180s per-row one).
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from app.core.agent.agent import SUBMIT_ANSWER_TOOL, Agent
from app.models.stages.llm_transform import (
    GRANTABLE_TOOLS,
    KNOWN_TOOL_NAMES,
    LLMConfig,
)
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


def test_a_stage_may_be_granted_the_open_web_and_nothing_on_this_machine():
    assert GRANTABLE_TOOLS == {"WebSearch", "WebFetch"}


def test_bash_still_loads_because_two_frozen_versions_carry_it():
    """The write-time refusal names Bash by name, so it must stay the only such name."""
    assert KNOWN_TOOL_NAMES - GRANTABLE_TOOLS == {"Bash"}
    assert _config(tools=["Bash"]).tools == ["Bash"]


def test_granting_bash_to_a_new_stage_is_refused():
    from app.services.stage_edit import find_ungrantable_tool_issues

    issues = find_ungrantable_tool_issues({"id": "research", "llm": {"tools": ["Bash"]}})
    assert issues and "`Bash`" in issues[0]


def test_a_stored_stage_that_already_grants_bash_stays_editable():
    from app.services.stage_edit import find_ungrantable_tool_issues

    candidate = {"id": "research", "llm": {"tools": ["Bash"], "temperature": 0.0}}
    assert find_ungrantable_tool_issues(candidate, {"research": {"llm": {"tools": ["Bash"]}}}) == []


# ── plumbing: tools reach the agent ──────────────────────────────────────────
def test_agent_grants_a_builtin_alongside_submit_answer():
    agent = Agent(
        system_prompt="s", target_schema=Reply, task="t",
        builtin_tools=["WebSearch"], max_turns=RESEARCH_MAX_TURNS,
    )
    engine = agent.build_engine()
    assert any(SUBMIT_ANSWER_TOOL in t for t in engine._allowed_tools)
    # On offer AND pre-approved: the SDK's `tools` is the set that exists at all, so a
    # name in allowed_tools alone is permission to call something nothing offers.
    assert "WebSearch" in engine._allowed_tools
    assert "WebSearch" in engine._builtin_tools
    assert engine._max_turns == RESEARCH_MAX_TURNS


def test_agent_without_builtin_tools_is_submit_only():
    engine = Agent(system_prompt="s", target_schema=Reply, task="t").build_engine()
    assert "WebSearch" not in engine._allowed_tools
    assert engine._builtin_tools == []  # every built-in off, not the SDK's default set


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
    assert seen["builtin_tools"] == ["WebSearch"]
    assert seen["max_turns"] == RESEARCH_MAX_TURNS
    assert seen["timeout"] == RESEARCH_TIMEOUT_S


def test_plain_row_keeps_the_cheap_budget(monkeypatch):
    seen = _capture(monkeypatch)
    runtime_llm.call_llm("s1", _config(), {"q": "2+2?"}, reply_model=Reply)
    assert seen["builtin_tools"] == []
    assert seen["max_turns"] is None
    assert seen["timeout"] == DEFAULT_TIMEOUT_S


# ── plumbing: how much the model reasons ─────────────────────────────────────
def test_a_stage_that_says_nothing_leaves_the_backend_setting_alone(monkeypatch):
    seen = _capture(monkeypatch)
    runtime_llm.call_llm("s1", _config(), {"q": "?"}, reply_model=Reply)
    assert seen["thinking"] is None


def test_a_stage_can_turn_the_reasoning_off(monkeypatch):
    # Reasoning nobody reads was 90% of one classifier stage's bill.
    seen = _capture(monkeypatch)
    runtime_llm.call_llm("s1", _config(thinking="disabled"), {"q": "?"}, reply_model=Reply)
    assert seen["thinking"] == {"type": "disabled"}


def test_the_setting_is_part_of_what_the_stage_computes():
    # It changes the answers, so a run under it must not read a cache filled without it.
    assert "thinking" in LLMConfig.FINGERPRINT_FIELDS
