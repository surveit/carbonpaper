"""Failures of the agent backend itself, as distinct from a bad answer."""
from __future__ import annotations

import anthropic
from claude_agent_sdk import ClaudeSDKError

from app.core.errors import StageWideFailure


class AccountLimitReached(ClaudeSDKError, StageWideFailure):
    """The account is out of allowance — about the account, not about this call."""
    # Also a ClaudeSDKError so the chat turn manager keeps reporting it as the
    # model failure it is, rather than letting an unhandled type escape.


class ApiKeyMissing(anthropic.AnthropicError):
    # An AnthropicError so a turn manager catching the SDK's errors reports this one too.

    def __init__(self, variable: str) -> None:
        super().__init__(
            f"{variable} is unset or empty; it holds the Anthropic API key the direct chat "
            "engine calls the Messages API with"
        )
