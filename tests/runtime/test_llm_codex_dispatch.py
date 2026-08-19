from __future__ import annotations

from typing import Literal

import pytest
from pydantic import BaseModel

import app.runtime.llm as llm
from app.core.errors import LLMError
from app.core.agent.usage import LlmUsage
from app.core.llm.options import LLMModel
from app.models.stages.llm_transform import LLMConfig


class Reply(BaseModel):
    verdict: Literal["supported"]


def _claude_config() -> LLMConfig:
    return LLMConfig(
        prompt_instructions="judge carefully",
        prompt_data_template="Rate: {text}",
        model=LLMModel.claude_haiku_4_5,
        max_retries=2,
    )


def _codex_config() -> LLMConfig:
    return LLMConfig(
        prompt_instructions="judge carefully",
        prompt_data_template="Rate: {text}",
        model=LLMModel.gpt_5_6_terra,
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
        *,
        usage_out: list[LlmUsage] | None = None,
    ) -> tuple[dict[str, object], LlmUsage]:
        calls.append((system_prompt, task, reply_model, model, max_retries, emit))
        if usage_out is not None:
            usage_out.append(expected_usage)
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


def test_codex_override_refuses_controls_the_backend_cannot_apply() -> None:
    config = LLMConfig(
        prompt_instructions="judge carefully",
        prompt_data_template="Rate: {text}",
        model=LLMModel.claude_haiku_4_5,
        tools=["WebSearch"],
        thinking="disabled",
    )

    with pytest.raises(LLMError) as exc_info:
        llm.call_llm(
            "judge",
            config,
            {"text": "x"},
            reply_model=Reply,
            model="gpt-5.6-terra",
        )

    assert "gpt-5.6-terra does not support llm.tools" in str(exc_info.value)
    assert "gpt-5.6-terra does not support llm.thinking" in str(exc_info.value)


def test_batch_codex_override_refuses_to_reach_the_agent_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_agent(*args, **kwargs):
        raise AssertionError("_run_agent should not run for a Codex batch")

    monkeypatch.setattr(llm, "_run_agent", forbidden_agent)

    with pytest.raises(LLMError, match="gpt-5.6-terra does not support batch execution"):
        llm.call_llm_batch(
            "judge",
            _claude_config(),
            instructions="judge carefully",
            task="### item 0\nRate: x",
            reply_schema=Reply,
            model="gpt-5.6-terra",
        )
