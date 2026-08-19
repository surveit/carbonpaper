from __future__ import annotations

import pytest
from pydantic import BaseModel

from app.core.agent.agent import Agent
from app.core.agent.codex_engine import CODEX_TRANSFORM_MODEL, CodexChatEngine
from app.core.errors import LLMError
from app.core.llm import LLMModel
from app.core.transform_model_settings import (
    initialize_transform_model_setting,
    set_transform_model,
)
from app.models.stages.llm_transform import LLMConfig
from app.runtime.llm import resolve_transform_model


def test_global_selected_model_overrides_a_stage_authored_model() -> None:
    initialize_transform_model_setting()
    set_transform_model(LLMModel.gpt_5_6_terra)

    selected = resolve_transform_model(LLMConfig(
        prompt_data_template="{question}", model=LLMModel.claude_opus_5
    ))

    assert selected == LLMModel.gpt_5_6_terra


def test_selected_codex_model_builds_a_codex_structured_output_engine() -> None:
    class Reply(BaseModel):
        verdict: str

    engine = Agent(
        system_prompt="system",
        target_schema=Reply,
        task="task",
        model=LLMModel.gpt_5_6_terra.value,
    ).build_engine()

    assert isinstance(engine, CodexChatEngine)
    assert engine._start_params()["model"] == LLMModel.gpt_5_6_terra.value


def test_codex_engine_model_matches_the_transform_model_catalog() -> None:
    assert CODEX_TRANSFORM_MODEL == LLMModel.gpt_5_6_terra.value


@pytest.mark.parametrize(
    ("config", "message"),
    [
        (LLMConfig(prompt_data_template="{question}", tools=["WebSearch"]), "tools"),
        (LLMConfig(prompt_data_template="{question}", batch_size=2), "batch_size=1"),
        (LLMConfig(prompt_data_template="{question}", thinking="disabled"), "thinking"),
    ],
)
def test_codex_selected_model_refuses_unsupported_stage_options(
    config: LLMConfig, message: str
) -> None:
    initialize_transform_model_setting()
    set_transform_model(LLMModel.gpt_5_6_terra)

    with pytest.raises(LLMError, match=message):
        resolve_transform_model(config)
