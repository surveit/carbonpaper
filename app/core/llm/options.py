"""Available LLM options.

The menu of models the platform may call. An org-specific deployment narrows or
extends this list; the workflow contract (app/models) references it so a stage can only
name a model the deployment actually offers.
"""
from enum import Enum


class LLMModel(str, Enum):
    haiku = "haiku"
    sonnet = "sonnet"
    opus = "opus"
    claude_sonnet_4_6 = "claude-sonnet-4-6"

    def __str__(self) -> str:
        """The wire id, not `LLMModel.x` — this is the string handed to the CLI."""
        return self.value
