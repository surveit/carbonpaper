"""Architecture: every `LLMModel` value names a model version, and `str(member)` is that
id — the string handed to the CLI. An unversioned alias (haiku/sonnet/opus) names a
different model after each Claude release, so every stage naming it would silently move,
carrying an unchanged definition fingerprint that no longer says what produced its rows.
"""
from __future__ import annotations

import re

from app.core.llm import LLMModel
from app.runtime.options import DEFAULT_MODEL

# `claude-<family>-<major>[-<minor>][-<snapshot date>]`: the published id forms that carry
# a version. A bare family name (`opus`) fails, as does anything not from this vendor.
_PINNED_ID = re.compile(r"^claude-[a-z]+(-\d+){1,3}$")


def test_every_model_id_names_a_version() -> None:
    unpinned = [member.value for member in LLMModel if not _PINNED_ID.fullmatch(member.value)]
    assert not unpinned, (
        f"LLMModel values that name no version: {unpinned}. A stage may only name a "
        "pinned id, so what it ran on stays legible after the next Claude release."
    )


def test_str_is_the_wire_id() -> None:
    # app.runtime.llm builds the CLI's model argument with str(); the enum default would
    # make that "LLMModel.claude_opus_5" and send a name no model answers to.
    assert [str(member) for member in LLMModel] == [member.value for member in LLMModel]


def test_the_runtime_default_is_a_pinned_model() -> None:
    # A stage omitting llm.model records nothing about which model answered, so this
    # default is the only remaining statement of what such a run's rows came from.
    assert isinstance(DEFAULT_MODEL, LLMModel)
