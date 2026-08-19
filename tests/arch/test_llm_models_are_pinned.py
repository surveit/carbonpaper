"""Architecture: every `LLMModel` value names a version, and `str(member)` is that id."""
from __future__ import annotations

import re

from app.core.llm import LLMModel
from app.runtime.options import DEFAULT_MODEL

_CLAUDE_PINNED_ID = re.compile(r"^claude-[a-z]+(-\d+){1,3}$")
_CODEX_PINNED_ID = re.compile(r"^gpt-\d+\.\d+-terra$")


def _names_version(value: str) -> bool:
    return (
        _CLAUDE_PINNED_ID.fullmatch(value) is not None
        or _CODEX_PINNED_ID.fullmatch(value) is not None
    )


def test_every_model_id_names_a_version() -> None:
    unpinned = [member.value for member in LLMModel if not _names_version(member.value)]
    assert not unpinned, (
        f"LLMModel values that name no version: {unpinned}. A stage may only name a "
        "pinned id, so what it ran on stays legible after the next Claude release."
    )


def test_str_is_the_wire_id() -> None:
    # The enum's default str() would send "LLMModel.claude_opus_5", a name no model answers to.
    assert [str(member) for member in LLMModel] == [member.value for member in LLMModel]


def test_the_runtime_default_is_a_pinned_model() -> None:
    assert isinstance(DEFAULT_MODEL, LLMModel)
