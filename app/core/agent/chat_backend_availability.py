"""Availability checks for chat backends."""
from __future__ import annotations

from app.core.agent.codex_availability import find_codex_backend_error
from app.core.agent.store import ChatBackend
from app.core.llm_sdk import CLI_PATH


def find_chat_backend_error(backend: ChatBackend) -> str | None:
    if backend == ChatBackend.claude:
        if CLI_PATH is not None:
            return None
        return "The Claude CLI / Agent SDK isn't available. Install it and run `claude login`."
    if backend == ChatBackend.codex:
        error = find_codex_backend_error()
        return str(error) if error is not None else None
    raise ValueError(f"unknown chat backend: {backend}")
