"""The menu of models the platform may call — pinned, version-bearing ids only. An
unversioned alias (haiku/sonnet/opus) resolves to whatever the CLI maps it to that week,
so the id a stage names would stop identifying the model that actually answered. A
deployment narrows or extends this list; the workflow contract (app/models) references it
so a stage can only name a model the deployment offers."""
from enum import Enum
from typing import Literal

LLMBackend = Literal["claude", "codex"]


class LLMModel(str, Enum):
    claude_haiku_4_5 = "claude-haiku-4-5"
    claude_sonnet_4_6 = "claude-sonnet-4-6"
    claude_sonnet_5 = "claude-sonnet-5"
    claude_opus_5 = "claude-opus-5"
    gpt_5_6_terra = "gpt-5.6-terra"

    def __str__(self) -> str:
        return self.value

    @property
    def backend(self) -> LLMBackend:
        if self is LLMModel.gpt_5_6_terra:
            return "codex"
        return "claude"

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
