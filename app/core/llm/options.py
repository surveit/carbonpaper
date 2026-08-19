"""The pinned model ids the platform can select for an LLM transform."""
import os
from enum import Enum


class LLMModel(str, Enum):
    claude_haiku_4_5 = "claude-haiku-4-5"
    claude_sonnet_4_6 = "claude-sonnet-4-6"
    claude_sonnet_5 = "claude-sonnet-5"
    claude_opus_5 = "claude-opus-5"
    gpt_5_6_terra = "gpt-5.6-terra"

    def __str__(self) -> str:
        return self.value

    @classmethod
    def parse(cls, value: str, *, source: str) -> "LLMModel":
        try:
            return cls(value)
        except ValueError as exc:
            raise ValueError(
                f"{source}={value!r} is not a model this deployment offers; choose one of "
                f"{[member.value for member in cls]}. An unversioned alias (haiku, sonnet, "
                f"opus) is refused on purpose: it names a different model after each release."
            ) from exc


DEFAULT_TRANSFORM_MODEL = LLMModel.parse(
    os.environ.get("CARBON_PAPER_LLM_MODEL", LLMModel.claude_haiku_4_5.value),
    source="CARBON_PAPER_LLM_MODEL",
)
