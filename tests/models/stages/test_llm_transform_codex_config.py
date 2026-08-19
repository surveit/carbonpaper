from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.llm import LLMModel
from app.models.stages.llm_transform import LLMConfig


def test_codex_model_selects_codex_backend() -> None:
    assert LLMModel.gpt_5_6_terra.backend == "codex"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tools", ["WebSearch"]),
        ("batch_size", 2),
        ("thinking", "adaptive"),
    ],
)
def test_codex_config_refuses_unimplemented_controls(field: str, value: object) -> None:
    with pytest.raises(
        ValidationError,
        match=rf"gpt-5\.6-terra.*llm\.{field}",
    ):
        LLMConfig.model_validate(
            {"model": "gpt-5.6-terra", "prompt_template": "judge {text}", field: value}
        )


def test_codex_config_accepts_implemented_controls() -> None:
    config = LLMConfig.model_validate(
        {"model": "gpt-5.6-terra", "prompt_template": "judge {text}"}
    )
    assert config.find_backend_capability_issues() == []


def test_claude_config_accepts_existing_controls() -> None:
    config = LLMConfig.model_validate(
        {
            "model": "claude-haiku-4-5",
            "prompt_template": "judge {text}",
            "tools": ["WebSearch"],
            "thinking": "adaptive",
        }
    )
    assert config.tools == ["WebSearch"]
