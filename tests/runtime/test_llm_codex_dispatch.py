from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

import app.runtime.llm as llm
from app.core.agent.usage import LlmUsage
from app.models.stages.llm_transform import LLMConfig


class Reply(BaseModel):
    verdict: Literal["supported"]


def _claude_config() -> LLMConfig:
    return LLMConfig(
        prompt_instructions="judge carefully",
        prompt_data_template="Rate: {text}",
        model="claude-haiku-4-5",
        max_retries=2,
    )


def _codex_config() -> LLMConfig:
    return LLMConfig(
        prompt_instructions="judge carefully",
        prompt_data_template="Rate: {text}",
        model="gpt-5.6-terra",
        max_retries=2,
    )


def test_one_row_codex_model_uses_codex_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, object, object, int, object]] = []
    usage_out: list[LlmUsage] = []
    expected_usage = LlmUsage(
        input_tokens=None,
        output_tokens=None,
        cost_usd=None,
        calls=1,
        model="gpt-5.6-terra",
    )

    def fake_codex(
        system_prompt: str,
        task: str,
        reply_model: type[BaseModel],
        model,
        max_retries: int,
        emit,
    ) -> tuple[dict[str, object], LlmUsage]:
        calls.append((system_prompt, task, reply_model, model, max_retries, emit))
        return {"verdict": "supported"}, expected_usage

    def forbidden_agent(*args, **kwargs):
        raise AssertionError("_run_agent should not run for a Codex model")

    monkeypatch.setattr(llm, "call_codex_transform", fake_codex)
    monkeypatch.setattr(llm, "_run_agent", forbidden_agent)

    result = llm.call_llm(
        "judge",
        _codex_config(),
        {"text": "x"},
        reply_model=Reply,
        usage_out=usage_out,
    )

    assert result == {"verdict": "supported"}
    assert usage_out == [expected_usage]
    assert calls == [
        (
            "You are executing one transform step of a data pipeline. Work from the "
            "task input you are given. Produce the required output by calling the "
            "submit_answer tool exactly once; its input schema is the required reply.\n\n"
            "judge carefully",
            "Rate: x",
            Reply,
            _codex_config().model,
            2,
            None,
        )
    ]


def test_claude_model_keeps_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_calls: list[tuple[str, str, object, str, int, object, object]] = []

    def fake_agent(
        system_prompt: str,
        task: str,
        target_schema: type[BaseModel],
        model_name: str,
        max_retries: int,
        usage_out,
        tools=None,
        thinking=None,
    ) -> dict[str, object]:
        agent_calls.append(
            (
                system_prompt,
                task,
                target_schema,
                model_name,
                max_retries,
                tools,
                thinking,
            )
        )
        return {"verdict": "supported"}

    def forbidden_codex(*args, **kwargs):
        raise AssertionError("call_codex_transform should not run for a Claude model")

    monkeypatch.setattr(llm, "_run_agent", fake_agent)
    monkeypatch.setattr(llm, "call_codex_transform", forbidden_codex)

    result = llm.call_llm(
        "judge",
        _claude_config(),
        {"text": "x"},
        reply_model=Reply,
    )

    assert result == {"verdict": "supported"}
    assert agent_calls == [
        (
            "You are executing one transform step of a data pipeline. Work from the "
            "task input you are given. Produce the required output by calling the "
            "submit_answer tool exactly once; its input schema is the required reply.\n\n"
            "judge carefully",
            "Rate: x",
            Reply,
            "claude-haiku-4-5",
            2,
            None,
            None,
        )
    ]


def test_batch_dispatch_stays_on_the_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_calls: list[tuple[str, str, object, str, int, object, object]] = []

    def fake_agent(
        system_prompt: str,
        task: str,
        target_schema: type[BaseModel],
        model_name: str,
        max_retries: int,
        usage_out,
        tools=None,
        thinking=None,
    ) -> dict[str, object]:
        agent_calls.append(
            (
                system_prompt,
                task,
                target_schema,
                model_name,
                max_retries,
                tools,
                thinking,
            )
        )
        return {"results": []}

    def forbidden_codex(*args, **kwargs):
        raise AssertionError("call_codex_transform should not run for a batch call")

    monkeypatch.setattr(llm, "_run_agent", fake_agent)
    monkeypatch.setattr(llm, "call_codex_transform", forbidden_codex)

    result = llm.call_llm_batch(
        "judge",
        _claude_config(),
        instructions="judge carefully",
        task="### item 0\nRate: x",
        reply_schema=Reply,
    )

    assert result == {"results": []}
    assert agent_calls == [
        (
            "You are executing one transform step of a data pipeline. Work from the "
            "task input you are given. Produce the required output by calling the "
            "submit_answer tool exactly once; its input schema is the required reply.\n\n"
            "judge carefully",
            "### item 0\nRate: x",
            Reply,
            "claude-haiku-4-5",
            2,
            None,
            None,
        )
    ]
