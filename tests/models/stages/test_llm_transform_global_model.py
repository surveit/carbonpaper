import pytest
from pydantic import ValidationError

from app.models.stages.llm_transform import LLMConfig


def test_codex_model_is_global_only_until_runtime_dispatch_supports_it():
    with pytest.raises(ValidationError, match="gpt-5.6-terra.*Admin"):
        LLMConfig(
            model="gpt-5.6-terra",
            prompt_template="judge {text}",
        )
